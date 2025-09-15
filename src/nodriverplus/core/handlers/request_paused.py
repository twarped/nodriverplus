"""
# **TODO/NOTE:**
intercepting requests this way for some reason stops sandboxed iframes from
being able to run scripts. with this enabled, Cloudflare will never solve if
you intercept the main page. (definitely an issue for PDF scraping)

fetch pause interception helpers (request + auth) layered over raw `nodriver` events.

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

quick start for response body streaming:
1. subclass `RequestPausedHandler`
2. override `should_intercept_response()` to return True for target requests
3. override `should_take_response_body_as_stream()` to return True for target responses
4. access captured bytes via `base64.b64decode(ev.body)` in `on_stream_finished()`

**important**: `ev.body` is always base64-encoded for Chrome CDP protocol compatibility.
always attach it to `ev` as `base64`, or `fulfill_request()` will fail.
"""

from __future__ import annotations

import base64
import logging
import asyncio
from dataclasses import dataclass, field, asdict

import nodriver
from nodriver import cdp

logger = logging.getLogger("nodriverplus.RequestPausedHandler")


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
    """per-event interception metadata.

    tracked fields:
    - `request_url`: current url for this paused event (updated across hops)
    - `nav_request_id`: CDP request id
    - `is_redirect`: whether this response is a redirect (status 3xx)
    - `redirect_target`: redirect destination (if any)
    - `streamed`: response body captured via streaming path
    - `request_will_be_sent_extra_info`: optional `RequestWillBeSentExtraInfo` event attached (if captured)
    """
    request_url: str
    nav_request_id: str
    is_redirect: bool = False
    redirect_target: str | None = None
    streamed: bool = False
    request_will_be_sent_extra_info: cdp.network.RequestWillBeSentExtraInfo | None = None

    @classmethod
    def from_event(cls, ev: cdp.fetch.RequestPaused) -> "RequestMeta":
        return cls(request_url=ev.request.url, nav_request_id=ev.request_id)

    def mark_redirect(self, target: str):
        self.is_redirect = True
        self.redirect_target = target

    def mark_streamed(self):
        self.streamed = True

    def to_dict(self):
        return asdict(self)


class RequestPausedHandler:
    """
    **NOTE:**
    - must be attached to a `TargetInterceptorManager` instance if
    you don't want to have to attach it manually to every tab you create.

    # overview:
    orchestrates request/response interception for a single tab.

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
    - override `should_*()` predicates for custom logic
    - override `on_stream_finish()` for post-stream transformations

    concurrency:
    - each intercepted event scheduled as a Task; `wait_for_tasks()` drains them on shutdown

    # streaming flow (for capturing response bodies' full byte-stream):
    to capture raw response bytes (e.g., PDFs, images) before Chrome processes them:

    1. override `should_intercept_response(ev)` to return True for requests you want to intercept responses for.
       this enables response phase interception during the request phase.

    2. when the response arrives, override `should_take_response_body_as_stream(ev)` to return True
       for responses whose bodies you want to capture as a stream.

    3. the framework will automatically call `take_response_body_as_stream(ev)`, which:
       - gets a stream handle from Chrome
       - reads all chunks into `ev.body` (base64-encoded)
       - calls `on_stream_finished(ev)` for post-processing
       - fulfills the request with the captured body

    4. access the captured bytes via `ev.body` (base64 string) or decode it: `base64.b64decode(ev.body)`

    **important**: `ev.body` must always be base64-encoded and attached to `ev` for `fulfill_request()` to work.
    Chrome's CDP protocol requires base64-encoded body data.

    example usage for PDF capture:

    ```python
    class PDFCaptureHandler(RequestPausedHandler):
        async def should_intercept_response(self, ev):
            # intercept responses for PDF URLs
            return ev.request.url.endswith('.pdf')

        async def should_take_response_body_as_stream(self, ev):
            # stream all intercepted responses
            return True

        async def on_stream_finished(self, ev):
            # save the PDF bytes - ev.body is base64-encoded, decode it first
            pdf_bytes = base64.b64decode(ev.body)
            with open('captured.pdf', 'wb') as f:
                f.write(pdf_bytes)
            # if you modify ev.body, it must remain base64-encoded for fulfill_request
            # ev.body = base64.b64encode(modified_pdf_bytes).decode()
    ```
    """
    # TODO:
    # i'm not sure how `binary_response_headers` work,
    # so some of that might need refactoring later on.

    nav_contexts: dict[str, NavigationContext]

    def __init__(self,
        tab: nodriver.Tab,
        capture_request_extra_info: bool = True
    ):
        """initialize a `RequestPausedHandler` for a given `Tab`.
        
        :param tab: the `Tab` to attach to.
        :param capture_request_extra_info: whether to capture `RequestWillBeSentExtraInfo` events.
        """
        self.tab = tab
        self.tasks: set[asyncio.Task] = set()
        # per-requestId navigation contexts (one logical chain per active id)
        # nav_contexts[request_id] = NavigationContext
        self.nav_contexts = {}
        # single lock is fine for tiny critical sections; keeps races away
        self._nav_lock = asyncio.Lock()
        # track RequestWillBeSentExtraInfo events (optional)
        self.capture_request_extra_info = capture_request_extra_info
        self._request_extra_info: dict[str, cdp.network.RequestWillBeSentExtraInfo] = {}
        self._extra_info_handler_added = False
        self._started = False


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
            meta = RequestMeta.from_event(ev)
            ev.meta = meta
        meta.request_url = ev.request.url
        meta.nav_request_id = ev.request_id
        # attach any captured RequestWillBeSentExtraInfo for downstream logic
        if self.capture_request_extra_info:
            extra = self._request_extra_info.get(ev.request_id)
            if extra is not None:
                meta.request_will_be_sent_extra_info = extra
                logger.debug("RequestWillBeSentExtraInfo for %s\n%s", ev.request.url, extra)



    # helper for tracking response redirects
    async def _annotate_response_navigation(self, ev: cdp.fetch.RequestPaused) -> bool:
        """annotate redirect metadata for this `request_id`; `True` when redirect handled.

        """
        meta = getattr(ev, "meta", None)
        if not isinstance(meta, RequestMeta):
            meta = RequestMeta.from_event(ev)
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
        meta.is_redirect = bool(is_redirect)
        meta.redirect_target = None
        meta.nav_request_id = ev.request_id

        if is_redirect and location:
            from urllib.parse import urljoin
            redirect_target = urljoin(ev.request.url, location)
            meta.mark_redirect(redirect_target)
            async with self._nav_lock:
                ctx.chain.append(redirect_target)
            return True
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

        :param ev: `cdp.fetch.RequestPaused` interception event (MUTATED IN-PLACE ACROSS FLOW).
        """
        pass


    async def should_fail_request(self, ev: cdp.fetch.RequestPaused) -> bool:
        """predicate deciding whether to abort the request.

        return True to short-circuit and send a fail_request; default False keeps it flowing.

        :param ev: interception event (mutable) used for decision logic.
        :return: bool flag to trigger `fail_request()`.
        :rtype: bool
        """
        return False
    

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

        **important**: if you plan to set `ev.body` in this method or downstream,
        it must be base64-encoded for `fulfill_request()` to work with Chrome's CDP protocol.

        :param ev: interception event (mutable) you may pre-populate with body / headers.
        :return: bool flag to trigger `fulfill_request()`.
        :rtype: bool
        """
        return False
    

    async def fulfill_request(self, ev: cdp.fetch.RequestPaused):
        """issue Fetch.fulfillRequest with fields extracted from `ev`.

        mutation note: body / headers may have been set by earlier steps (e.g. stream capture).

        **important**: if providing a body, `ev.body` must be base64-encoded as required by Chrome's CDP protocol.
        The body data will be sent as-is to fulfill the request.

        :param ev: interception event containing response parameters (MUTATED PRIOR).

        expected `ev` state:
        - `request_id`: str
        - `response_status_code`: int status to emit
        - `response_status_text`: (optional) str status text
        - `response_headers`: list[HeaderEntry]
        - `binary_response_headers`: (optional)
        - `body`: (optional base64-encoded string) — must be base64 for CDP compatibility
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


    async def should_intercept_response(self, ev: cdp.fetch.RequestPaused) -> bool:
        """predicate controlling whether to intercept the response phase.

        return `True` to enable response phase interception (`response_*` fields populated);
        default False lets the response flow through unmodified.

        ### this is called during the request phase to decide if we should pause the response.
        only when this returns `True` will response-phase
        methods like `on_response()` and `should_take_response_body_as_stream()` be called.

        example: only intercept responses whose URL includes "jim":

        ```python
        async def should_intercept_response(self, ev):
            return "jim" in ev.request.url
        ```

        :param ev: interception event (mutable) used for decision logic.
        :return: bool flag to trigger response phase interception. (`on_response()` etc.)
        :rtype: bool
        """
        return False


    async def continue_request(self, ev: cdp.fetch.RequestPaused):
        """allow the original request to proceed untouched (or lightly adjusted).

        can optionally mutate `ev.request` before continuing.

        also, if you override this method, ensure that `ev.request.post_data` is
        base64-encoded if present, otherwise CDP will throw.

        :param ev: interception event referencing the paused network request.
        """
        # chrome expects postData param to be base64 per Fetch.continueRequest spec
        post_data = ev.request.post_data
        if post_data is not None:
            try:
                # try to detect if already valid base64; if not, encode
                base64.b64decode(post_data, validate=True)
            except Exception:
                if isinstance(post_data, str):
                    post_data = base64.b64encode(post_data.encode()).decode()
                elif isinstance(post_data, (bytes, bytearray)):
                    post_data = base64.b64encode(bytes(post_data)).decode()
                else:
                    # unknown type - skip encoding and let chrome handle (will likely fail)
                    logger.debug("unexpected post_data type for %s: %r", ev.request.url, type(post_data))
        header_entries = [cdp.fetch.HeaderEntry(name=key, value=value) for key, value in ev.request.headers.items()]
        logger.debug("continuing request for %s", ev.request.url)
        try:
            await self.tab.send(
                cdp.fetch.continue_request(
                    ev.request_id,
                    ev.request.url,
                    ev.request.method,
                    post_data,
                    header_entries,
                    bool(await self.should_intercept_response(ev))
                )
            )
        except Exception as e:
            if "[-32" in str(e):
                logger.debug("failed to continue request for %s:\n  %s",
                    ev.request.url, e)
        logger.debug("successfully continued request for %s", ev.request.url)


    async def should_take_response_body_as_stream(self, ev: cdp.fetch.RequestPaused) -> bool:
        """predicate for streaming response bodies before chrome consumes them.

        **NOTE**: this is only invoked when `should_intercept_response()` returns `True`.

        use when you need raw bytes (pdf, media) or want to transform before fulfill.
        when `True`, the response body will be captured as a stream and stored in `ev.body`
        (base64-encoded), then the request will be fulfilled with that body.

        this happens during the response phase, after `on_response()` is called.

        example: stream all PDF responses:

        ```python
        async def should_take_response_body_as_stream(self, ev):
            content_type = None
            for header in ev.response_headers or []:
                if header.name.lower() == 'content-type':
                    content_type = header.value.lower()
                    break
            return 'application/pdf' in content_type
        ```

        :param ev: interception event (mutable) used to decide streaming.
        :return: bool to trigger `take_response_body_as_stream()`.
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

        **important**: `ev.body` is always set as a base64-encoded string for CDP protocol compatibility.
        this is required for `fulfill_request()` to work properly.

        mutation note: sets `ev.body` which `fulfill_request()` later reuses.

        override this method if you need custom stream processing (e.g., partial reads,
        compression, or avoiding full buffering for large files).

        example: save large files directly to disk without buffering in memory:

        ```python
        async def handle_response_body_stream(self, ev, stream):
            with open('large_file.pdf', 'wb') as f:
                while True:
                    b64, data, eof = await self.tab.send(cdp.io.read(handle=stream))
                    chunk = base64.b64decode(data) if b64 else bytes(data, "utf-8")
                    f.write(chunk)
                    if eof: break
            # IMPORTANT: ev.body must be set to base64-encoded data for fulfill_request to work
            # even if saving to disk, provide empty base64 body or chrome will reject
            ev.body = base64.b64encode(b'').decode()
        ```

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

        this is called after the stream has been fully read and `ev.body` is set.
        use this for post-processing like decoding, validation, or side effects.

        **important**: `ev.body` must remain base64-encoded for `fulfill_request()` to work.
        if you modify the body data, re-encode it: `ev.body = base64.b64encode(new_bytes).decode()`

        example: decode and validate PDF content:

        ```python
        async def on_stream_finished(self, ev):
            pdf_bytes = base64.b64decode(ev.body)
            if not pdf_bytes.startswith(b'%PDF-'):
                logger.warning("received non-PDF content for %s", ev.request.url)
            # IMPORTANT: if you modify the body, re-encode it to base64 for fulfill_request
            # ev.body = base64.b64encode(modified_bytes).decode()
        ```

        :param ev: interception event mutated with body.
        """
        pass


    async def take_response_body_as_stream(self, ev: cdp.fetch.RequestPaused):
        """wrapper performing takeResponseBodyAsStream + buffering + closure.

        attaches processed base64-encoded body to `ev.body` for subsequent fulfill.

        **NOTE**: override `on_stream_finished()` to **easily access or modify** `ev.body` before
        fulfillment *without* modifying this function or `handle_response_body_stream()`.

        this method is called automatically when `should_take_response_body_as_stream()`
        returns `True`.

        flow:
        1. request stream handle from chrome
        2. call `handle_response_body_stream()` to read chunks into `ev.body`
        3. call `on_stream_finished()` for post-processing
        4. close the stream handle
        5. mark the event as streamed to prevent double-processing

        **important**:
        - `ev.body` must remain base64-encoded for `fulfill_request()` to work with chrome's CDP protocol.

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
            meta = RequestMeta.from_event(ev)
            ev.meta = meta  # type: ignore[attr-defined]
        meta.mark_streamed()


    async def on_response(self, ev: cdp.fetch.RequestPaused):
        """hook invoked after a response is available (response phase interception).

        **NOTE**: only invoked when `should_intercept_response()` returns True.
        
        extend to inspect headers / status / decide transformation before continue_response.

        :param ev: interception event (contains response_* fields; may be mutated).
        """
        pass


    async def continue_response(self, ev: cdp.fetch.RequestPaused):
        """resume the response flow with minimal interference.

        can be overridden to rewrite headers or status before forwarding.

        :param ev: interception event carrying response metadata.

        expected `ev` state:
        - `request_id`: str
        - `response_status_code`: int status to emit
        - `response_status_text`: optional status text
        - `response_headers`: optional `list[HeaderEntry]`
        - `binary_response_headers`: optional `bytes` for raw headers
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
        - if no response yet (request phase):
          1. annotate navigation state
          2. call `on_request(ev)`
          3. check predicates in order: fail -> fulfill -> continue
             (continue calls `should_intercept_response()` to enable response interception)

        - if response available (response phase):
          1. annotate navigation state
          2. call `on_response(ev)`
          3. if redirect: `continue_response`
          4. elif `should_take_response_body_as_stream()`: 
             stream body -> `fulfill_request`
          5. else: `continue_response`
          6. clean up navigation context

        mutation note: `ev` may receive added or mutated
        fields (`body`, `binary_response_headers`, etc.).

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
                logger.debug("detected redirect: %s => %s", 
                    self.nav_contexts[ev.request_id].chain[-1], 
                    ev.request.url
                )
                await self.continue_response(ev)
            elif await self.should_take_response_body_as_stream(ev):
                await self.take_response_body_as_stream(ev)
                await self.fulfill_request(ev)
            else:
                await self.continue_response(ev)
            # cleanup context after final (non-redirect) response
            if isinstance(meta, RequestMeta) and not meta.is_redirect:
                async with self._nav_lock:
                    self.nav_contexts.pop(ev.request_id, None)


    def handle(self, ev: cdp.fetch.RequestPaused):
        """public entry: schedule `_handle` as a task for async concurrency.

        :param ev: interception event queued for processing.
        """
        task = asyncio.create_task(self._handle(ev))
        self.tasks.add(task)
        task.add_done_callback(lambda t: self.tasks.discard(t))


    async def wait_for_tasks(self):
        """await all outstanding interception tasks."""
        logger.info("waiting for pending tasks to finish")
        await asyncio.gather(*self.tasks, return_exceptions=True)
        logger.info("all pending tasks finished")


    def _store_request_will_be_sent_extra_info(self, ev: cdp.network.RequestWillBeSentExtraInfo):
        # store latest; do not purge immediately (fetch events can arrive after Network events)
        self._request_extra_info[ev.request_id] = ev


    async def start(self):
        """
        start request interception on the current tab and config
        """
        if self._started:
            return
        self._started = True
        await self.tab.send(cdp.fetch.enable([
            cdp.fetch.RequestPattern(url_pattern="*")
        ]))
        await self.tab.send(cdp.network.enable())
        logger.debug("enabled domains: %s", self.tab.enabled_domains)
        if cdp.fetch not in self.tab.enabled_domains:
            self.tab.enabled_domains.append(cdp.fetch)
        if cdp.network not in self.tab.enabled_domains:
            self.tab.enabled_domains.append(cdp.network)
        # ensure chrome always loads fresh bytes
        # await self.tab.send(cdp.network.set_cache_disabled(True))
        self.tab.add_handler(cdp.fetch.RequestPaused, self.handle)
        if self.capture_request_extra_info and not self._extra_info_handler_added:
            # capture RequestWillBeSentExtraInfo so that paused events can reference original headers
            self.tab.add_handler(cdp.network.RequestWillBeSentExtraInfo, self._store_request_will_be_sent_extra_info)
            self._extra_info_handler_added = True


    async def stop(self, remove_handler = True, wait_for_tasks = True):
        """
        stop the request interception and wait for pending tasks if specified

        :param remove_handler: whether to remove the `RequestPaused` handler
        :param wait_for_tasks: whether to wait for outstanding tasks to complete
        """
        if remove_handler:
            self.tab.remove_handler(cdp.fetch.RequestPaused, self.handle)
            if self._extra_info_handler_added:
                self.tab.remove_handler(cdp.network.RequestWillBeSentExtraInfo, self._store_request_will_be_sent_extra_info)
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
