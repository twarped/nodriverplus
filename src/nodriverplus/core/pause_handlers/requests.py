"""fetch pause interception helpers (request + auth) layered over raw `nodriver` events.

adds structured async hooks around `Fetch.requestPaused` / `Fetch.authRequired` so callers can:
- inspect / mutate request + response phases (fail, fulfill, stream, continue)
- stream binary bodies prior to chrome consumption (e.g. pdf) then re-serve via fulfill
- inject auth challenge responses (default or credentials) before continuing
- stage mutations directly on the event object (`ev`) which is threaded across the decision tree

design notes:
- `ev` is treated as shared mutable state (body, headers, auth_challenge_response, etc.)
- predicates (`should_*`) are small override points to steer control flow
- separation of streaming (`take_response_body_as_stream`) from fulfillment keeps memory + clarity
- tasks set prevents racy shutdown by awaiting outstanding async handler work
"""

from __future__ import annotations

import base64
import logging
import asyncio
from dataclasses import dataclass, field, asdict

import nodriver
from nodriver import cdp

logger = logging.getLogger(__name__)


@dataclass
class NavigationContext:
    """navigation context for a logical request chain.

    attributes:
    - current: the initial URL for this request chain (first hop)
    - chain: redirect targets encountered (in order)
    - final: final resolved URL once a non-redirect main response completes
    - done: flag indicating the main navigation finished
    """
    current: str
    chain: list[str] = field(default_factory=list)
    final: str | None = None
    done: bool = False


@dataclass
class RequestMeta:
    """typed per-event interception metadata.

    fields:
    - request_url: url (may update across hops)
    - nav_request_id: cdp request id
    - is_main_candidate: set during request phase when it might become main nav
    - is_main: resolved main navigation flag (response phase)
    - is_redirect: this event is a redirect (3xx)
    - redirect_target: resolved redirect location (absolute)
    - final_main: final non-redirect main navigation completed
    - streamed: response body captured via streaming path
    """
    request_url: str
    nav_request_id: str
    is_main_candidate: bool = False
    is_main: bool = False
    is_redirect: bool = False
    redirect_target: str | None = None
    final_main: bool = False
    streamed: bool = False

    @classmethod
    def from_request(cls, ev: cdp.fetch.RequestPaused) -> "RequestMeta":
        """create a RequestMeta initialized from a request-paused event.

        copies the current `ev.request.url` and `ev.request_id` into a new
        RequestMeta instance so callers can attach it to `ev.meta`.
        """
        return cls(request_url=ev.request.url, nav_request_id=ev.request_id)

    def mark_main_candidate(self, flag: bool):
        """mark whether this paused request is a main-navigation candidate.

        flag - True when the URL appears to be the main navigation for this chain.
        """
        self.is_main_candidate = flag

    def mark_redirect(self, target: str):
        """record a redirect hop for this event and set the redirect target.

        target - absolute redirect destination (typically urljoin result).
        """
        self.is_redirect = True
        self.redirect_target = target

    def mark_final_main(self):
        """mark this event as the final non-redirect main navigation.

        this flips both `final_main` and `is_main` so downstream logic can
        decide to stream or purge context reliably.
        """
        self.final_main = True
        self.is_main = True

    def mark_streamed(self):
        """indicate the response body has been captured via streaming.

        used to avoid double-processing the body on races.
        """
        self.streamed = True

    def to_dict(self):
        """return a shallow dict representation for logging or serialization."""
        return asdict(self)


class RequestPausedHandler:
    """orchestrates request/response interception for a single tab.

    **note**: can remove the `Range` header from requests in `_annotate_request_navigation()`

    lifecycle (request phase):
    1. `on_request(ev)` - inspect / prep state
    2. predicates in order: fail -> fulfill -> stream -> continue
    3. optional stream capture mutates `ev.body` then fulfill

    response phase:
    - `on_response(ev)` then `continue_response(ev)` (override to rewrite headers/status)

    mutation contract:
    - `ev` is mutated in-place (body, headers, etc.) and passed downstream
    - overrides should avoid replacing `ev` wholesale; adjust fields so later steps see changes

    extension points:
    - override `should_*` predicates for custom logic
    - override `handle_response_body` for post-stream transformations

    concurrency:
    - each intercepted event scheduled as a Task; `wait_for_tasks()` drains them on shutdown
    """
    # TODO:
    # i'm not sure how `binary_response_headers` work,
    # so some of that might need refactoring later on.

    tab: nodriver.Tab
    tasks: set[asyncio.Task]
    remove_range_header: bool

    nav_contexts: dict[str, NavigationContext]

    def __init__(self, tab: nodriver.Tab, remove_range_header = True):
        self.tab = tab
        self.remove_range_header = remove_range_header
        self.tasks = set()
        # per-requestId navigation contexts (one logical chain per active id)
        # nav_contexts[request_id] = NavigationContext
        self.nav_contexts = {}
        # single lock is fine for tiny critical sections; keeps races away
        self._nav_lock = asyncio.Lock()


    # helper for tracking request redirects
    async def _annotate_request_navigation(self, ev: cdp.fetch.RequestPaused):
        async with self._nav_lock:
            ctx = self.nav_contexts.get(ev.request_id)
            if ctx is None:
                # first hop for this logical chain
                ctx = NavigationContext(current=ev.request.url)
                self.nav_contexts[ev.request_id] = ctx
        meta = getattr(ev, "meta", None)
        if not isinstance(meta, RequestMeta):
            meta = RequestMeta.from_request(ev)
            ev.meta = meta
        meta.request_url = ev.request.url
        meta.nav_request_id = ev.request_id
        meta.mark_main_candidate(ev.request.url == ctx.current and not ctx.done)
        # remove Range header to avoid servers sending 206 partial responses
        if self.remove_range_header:
            for k in list(ev.request.headers.keys()):
                if k.lower() == "range":
                    ev.request.headers.pop(k, None)



    # helper for tracking response redirects
    async def _annotate_response_navigation(self, ev: cdp.fetch.RequestPaused) -> bool:
        """annotate redirect + main flags for this requestId; True when redirect handled.

        per-event meta keys:
        - is_main
        - is_redirect
        - redirect_target
        - final_main
        - nav_request_id
        also sets meta['purge_nav_ctx'] when final main completes so caller can delete context.
        """
        meta = getattr(ev, "meta", None)
        if not isinstance(meta, RequestMeta):
            meta = RequestMeta.from_request(ev)
            ev.meta = meta
        async with self._nav_lock:
            ctx = self.nav_contexts.get(ev.request_id)
            if ctx is None:
                # late response without prior request phase (edge) create minimal context
                ctx = NavigationContext(current=ev.request.url)
                self.nav_contexts[ev.request_id] = ctx
        status = ev.response_status_code or 0
        location = None
        if ev.response_headers:
            for h in ev.response_headers:
                if h.name.lower() == "location":
                    location = h.value
                    break
        is_redirect = 300 <= status < 400
        is_main = (ev.request.url == ctx.current and not ctx.done) or meta.is_main_candidate
        meta.is_main = bool(is_main)
        meta.is_redirect = bool(is_redirect)
        meta.redirect_target = None
        meta.final_main = False
        meta.nav_request_id = ev.request_id

        if is_main and is_redirect and location:
            from urllib.parse import urljoin
            redirect_target = urljoin(ev.request.url, location)
            meta.mark_redirect(redirect_target)
            async with self._nav_lock:
                ctx.chain.append(redirect_target)
            return True
        if is_main and not is_redirect and not ctx.done:
            async with self._nav_lock:
                ctx.final = ev.request.url
                ctx.done = True
            meta.mark_final_main()
        return False


    async def on_request(self, ev: cdp.fetch.RequestPaused):
        """invoked for every paused network request phase prior to a response.

        purpose:
        - inspect / mutate the intercepted request event (`ev`) before deciding a control path
        - lightweight hook for logging / future header tweaks

        mutation note:
        `ev` is a live event object that may be mutated downstream across handler steps
        (e.g. body injection, auth challenge response). we treat it as state passed through
        the decision tree.

        :param ev: cdp.fetch.RequestPaused interception event (MUTATED IN-PLACE ACROSS FLOW).
        """
        pass


    async def should_fail_request(self, ev: cdp.fetch.RequestPaused) -> bool:
        """predicate deciding whether to abort the request.

        return True to short-circuit and send a fail_request; default False keeps it flowing.

        :param ev: interception event (mutable) used for decision logic.
        :return: bool flag to trigger fail_request.
        """
        return False
    

    # TODO:
    # should it be `response_error_reason` or `error_reason`?
    # or even `request_error_reason`?
    async def fail_request(self, ev: cdp.fetch.RequestPaused):
        """send a Fetch.failRequest CDP command for the paused request.

        assumes `ev.response_error_reason` is populated / acceptable to chrome.

        mutation note: `ev` not mutated here; we only read its fields.

        :param ev: interception event slated for failure.
        """
        await self.tab.send(
            cdp.fetch.fail_request(
                ev.request_id,
                ev.response_error_reason
            )
        )


    async def should_fulfill_request(self, ev: cdp.fetch.RequestPaused) -> bool:
        """predicate controlling full synthetic response injection.

        return True when we intend to build / modify a response via fulfill_request.

        :param ev: interception event (mutable) you may pre-populate with body / headers.
        :return: bool flag to trigger fulfill_request.
        """
        return False
    

    # TODO:
    # should it be `response_status_code` or `response_code`?
    # should it be `response_status_text` or `response_phrase`?
    async def fulfill_request(self, ev: cdp.fetch.RequestPaused):
        """issue Fetch.fulfillRequest with fields extracted from `ev`.

        expected `ev` state:
        - response_status_code: int status to emit
        - response_headers: list[HeaderEntry]
        - binary_response_headers (optional)
        - body (optional base64) — mutated previously (e.g. by streaming helpers)

        mutation note: body / headers may have been set by earlier steps (e.g. stream capture).

        :param ev: interception event containing response parameters (MUTATED PRIOR).
        """
        await self.tab.send(
            cdp.fetch.fulfill_request(
                ev.request_id,
                ev.response_status_code,
                ev.response_headers,
                getattr(ev, 'binary_response_headers', None),
                getattr(ev, 'body', None),
                ev.response_status_text or None
            )
        )
        logger.debug("successfully fulfilled request for %s", ev.request.url)


    async def continue_request(self, ev: cdp.fetch.RequestPaused):
        """allow the original request to proceed untouched (or lightly adjusted).

        can optionally mutate `ev.request` before continuing.

        :param ev: interception event referencing the paused network request.
        """
        await self.tab.send(
            cdp.fetch.continue_request(
                ev.request_id,
                ev.request.url,
                ev.request.method,
                ev.request.post_data,
                [cdp.fetch.HeaderEntry(name=key, value=value) for key, value in ev.request.headers.items()],
                True
            )
        )


    async def should_take_response_body_as_stream(self, ev: cdp.fetch.RequestPaused) -> bool:
        """predicate for streaming response bodies before chrome consumes them.

        use when you need raw bytes (pdf, media) or want to transform before fulfill.

        happens during request interception

        :param ev: interception event (mutable) used to decide streaming.
        :return: bool to trigger take_response_body_as_stream.
        :rtype: bool
        """
        return False


    async def handle_response_body_stream(self,
        ev: cdp.fetch.RequestPaused,
        stream: cdp.io.StreamHandle
    ):
        """read an IO stream into memory and attach base64 body onto `ev`.

        flow:
        1. iteratively read chunks via IO.read
        2. decode base64 when flagged (cdp returns a pair (b64flag,data,...))
        3. aggregate, then assign `ev.body` (base64 re-encoded)

        mutation note: sets `ev.body` which later fulfill_request reuses.

        :param ev: interception event mutated with body.
        :param stream: stream handle from Fetch.takeResponseBodyAsStream.
        """
        buf = bytearray()
        while True:
            b64, data, eof = await self.tab.send(cdp.io.read(handle=stream))
            buf.extend(base64.b64decode(data) if b64 else bytes(data, "utf-8"))
            if eof: break
        ev.body = base64.b64encode(buf).decode()


    async def on_stream_finished(self, ev: cdp.fetch.RequestPaused):
        """optional override for managing `ev.body` before fulfillment
        without having to modify `take_response_body_as_stream()` or
        `handle_response_body_stream()`.

        :param ev: interception event mutated with body.
        """
        pass


    async def take_response_body_as_stream(self, ev: cdp.fetch.RequestPaused):
        """wrapper performing takeResponseBodyAsStream + buffering + closure.

        mutation note: attaches processed base64 body to `ev.body` for subsequent fulfill.

        override `on_stream_finished()` to easily access or modify `ev.body` before
        fulfillment without modifying this function or `handle_response_body_stream()`.

        :param ev: interception event mutated with body.
        """
        stream = await self.tab.send(
            cdp.fetch.take_response_body_as_stream(ev.request_id)
        )
        await self.handle_response_body_stream(ev, stream)
        await self.on_stream_finished(ev)
        await self.tab.send(cdp.io.close(stream))
        # mark streamed so we never double-handle body on late races
        meta = getattr(ev, "meta", None)
        if not isinstance(meta, RequestMeta):
            meta = RequestMeta.from_request(ev)
            ev.meta = meta  # type: ignore[attr-defined]
        meta.mark_streamed()


    async def on_response(self, ev: cdp.fetch.RequestPaused):
        """hook invoked after a response is available (response phase interception).

        extend to inspect headers / status / decide transformation before continue_response.

        :param ev: interception event (contains response_* fields; may be mutated).
        """
        pass


    # TODO:
    # should it be `response_status_code` or `response_code`?
    # should it be `response_status_text` or `response_phrase`?
    async def continue_response(self, ev: cdp.fetch.RequestPaused):
        """resume the response flow with minimal interference.

        can be overridden to rewrite headers or status before forwarding.

        :param ev: interception event carrying response metadata.
        """
        await self.tab.send(
            cdp.fetch.continue_response(
                ev.request_id,
                ev.response_status_code,
                ev.response_status_text or None,
                ev.response_headers,
                getattr(ev, 'binary_response_headers', None),
            )
        )


    async def _handle(self, ev: cdp.fetch.RequestPaused):
        """internal dispatcher orchestrating request vs response phases.

        decision tree:
        - if no response yet: run on_request -> predicates (fail | fulfill | stream | continue)
        - if response: on_response -> continue_response

        mutation note: `ev` may receive added fields (body, binary_response_headers, etc.).

        :param ev: interception event being processed (MUTATED ACROSS STEPS).
        """
        if ev.response_status_code is None:
            logger.debug("successfully intercepted request for %s", ev.request.url)
            # annotate request navigation state before predicates
            await self._annotate_request_navigation(ev)
            await self.on_request(ev)
            if await self.should_fail_request(ev):
                await self.fail_request(ev)
                return
            if await self.should_fulfill_request(ev):
                await self.fulfill_request(ev)
                return
            await self.continue_request(ev)
        else:
            logger.debug("successfully intercepted response for %s", ev.request.url)
            # annotate response navigation
            redirect = await self._annotate_response_navigation(ev)
            await self.on_response(ev)
            meta = getattr(ev, "meta", None)
            if redirect:
                # just pass through redirects (no streaming / fulfill)
                await self.continue_response(ev)
                logger.debug("detected redirect: %s => %s", 
                    self.nav_contexts[ev.request_id].chain[-1], 
                    ev.request.url
                )
            elif isinstance(meta, RequestMeta) and meta.final_main and await self.should_take_response_body_as_stream(ev):
                await self.take_response_body_as_stream(ev)
                await self.fulfill_request(ev)
            else:
                await self.continue_response(ev)
            if isinstance(meta, RequestMeta) and meta.final_main:
                async with self._nav_lock:
                    self.nav_contexts.pop(ev.request_id, None)


    def handle(self, ev: cdp.fetch.RequestPaused):
        """public entry: schedule `_handle` as a task for async concurrency.

        :param ev: interception event queued for processing.
        """
        task = asyncio.create_task(self._handle(ev))
        self.tasks.add(task)
        task.add_done_callback(lambda t: self.tasks.discard(t))


    def __call__(self, ev: cdp.fetch.RequestPaused):
        self.handle(ev)


    async def wait_for_tasks(self):
        """await all outstanding interception tasks."""
        logger.info("waiting for pending tasks to finish")
        await asyncio.gather(*self.tasks, return_exceptions=True)


    async def start(self):
        """
        start request interception on the current tab and config
        """
        await self.tab.send(cdp.fetch.enable())
        await self.tab.send(cdp.network.enable())
        # ensure chrome always loads fresh bytes
        await self.tab.send(cdp.network.set_cache_disabled(True))
        self.tab.add_handler(cdp.fetch.RequestPaused, self.handle)


    async def stop(self, remove_handler = True, wait_for_tasks = True):
        """
        stop the request interception and wait for pending tasks if specified

        :param remove_handler: whether to remove the `RequestPaused` handler
        :param wait_for_tasks: whether to wait for outstanding tasks to complete
        """
        if remove_handler:
            self.tab.remove_handler(cdp.fetch.RequestPaused, self.handle)
        if wait_for_tasks:
            await self.wait_for_tasks()


# TODO: make this actually work
class AuthRequiredHandler:
    """API/handler for `Fetch.authRequired` challenges.

    flow:
    1. `on_auth_required(ev)` attaches an auth_challenge_response (mutates `ev`)
    2. `continue_with_auth(ev)` sends decision to chrome

    mutation contract:
    - `ev.auth_challenge_response` is set in-place; callers can override to provide credentials

    concurrency mirrors RequestPausedHandler: events become Tasks tracked in `tasks`.
    """

    connection: nodriver.Tab | nodriver.Connection
    tasks: set[asyncio.Task]

    def __init__(self, connection: nodriver.Tab | nodriver.Connection):
        self.connection = connection
        self.tasks = set()


    async def on_auth_required(self, ev: cdp.fetch.AuthRequired):
        """invoked when a request triggers an authentication challenge.

        default strategy: answer with a "Default" challenge response (let browser decide).

        extend to inject credentials:
        ev.auth_challenge_response = cdp.fetch.AuthChallengeResponse("ProvideCredentials", username=..., password=...)

        mutation note: sets `ev.auth_challenge_response` consumed in continue_with_auth.

        :param ev: auth challenge event (MUTATED with response object).
        """
        logger.info("auth required: %s", ev.request.url)
        ev.auth_challenge_response = cdp.fetch.AuthChallengeResponse("Default")


    async def continue_with_auth(self, ev: cdp.fetch.AuthRequired):
        """issue continueWithAuth using the response prepared on `ev`.

        precondition: on_auth_required must set ev.auth_challenge_response.

        :param ev: auth challenge event carrying previously attached response.
        """
        await self.connection.send(
            cdp.fetch.continue_with_auth(
                ev.request_id,
                # throw if auth_challenge_response is not present
                ev.auth_challenge_response
            )
        )

    
    async def _handle(self, ev: cdp.fetch.AuthRequired):
        """orchestrate auth flow: prepare challenge response then continue.

        :param ev: auth event (MUTATED then forwarded).
        """
        await self.on_auth_required(ev)
        await self.continue_with_auth(ev)


    async def handle(self, ev: cdp.fetch.AuthRequired):
        """public entry: schedule `_handle` as a task for async concurrency.

        :param ev: auth challenge event queued for processing.
        """
        task = asyncio.create_task(self._handle(ev))
        self.tasks.add(task)
        task.add_done_callback(lambda t: self.tasks.discard(t))


    async def wait_for_tasks(self):
        """await all outstanding auth tasks."""
        await asyncio.gather(*self.tasks, return_exceptions=True)


    async def start(self):
        pass


    async def stop(self):
        await self.wait_for_tasks()
