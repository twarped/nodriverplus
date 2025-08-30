from __future__ import annotations

from dataclasses import dataclass, field
import base64
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

import nodriver
from nodriver import cdp

logger = logging.getLogger(__name__)

# data containers ------------------------------

@dataclass(frozen=True)
class RequestInterceptionConfig:
    """static interception options (immutable)"""
    capture_main_body: bool = True
    set_referer: bool = True
    text_tokens: tuple[str, ...] = ("text", "javascript", "json", "xml")
    max_redirects: int = 10

@dataclass
class RequestInterceptionState:
    """mutable runtime facts kept separate from config"""
    main_url_initial: str | None = None
    main_url_current: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    main_body_done: bool = False
    main_body_bytes: bytes | None = None
    streamed_mime: str | None = None
    # debugging / inspection
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    range_chunks: list[tuple[int, bytes]] = field(default_factory=list)
    range_total: int | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "main_url_initial": self.main_url_initial,
            "main_url_current": self.main_url_current,
            "redirects": len(self.redirect_chain),
            "main_body_done": self.main_body_done,
            "bytes": len(self.main_body_bytes) if self.main_body_bytes else 0,
            "mime": self.streamed_mime,
        }

# hook infrastructure ---------------------

Hook = Callable[..., Awaitable[any]]

RE_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")

class RequestPausedHandler:
    """fetch.requestPaused interception pipeline.

    `config`: immutable options (referer, streaming policy)

    `state`: runtime facts (main url, redirects, body bytes, flags)

    `hooks`: async callables bound to phases

    `phases` (order):
    - request_prepare
    - request_sent
    - response_headers
    - redirect (zero+ until final)
    - should_stream
    - stream_start
    - stream_complete
    - response_done
    - error
    - finalize

    default:
    - inject referer when missing
    - stream main non-text body
    - track redirects
    - assemble simple 206 ranges

    usage:
    ```
    handler = RequestPausedHandler()
    connection.add_handler(cdp.fetch.RequestPaused, handler.handle)
    ```

    customization: `handler.add_hook("request_prepare", fn)`
    """

    def __init__(self, *, config: RequestInterceptionConfig | None = None):
        self.config = config or RequestInterceptionConfig()
        self.state = RequestInterceptionState()
        self._fetch_enabled = False
        # phase -> list[Hook]
        self._hooks: dict[str, list[Hook]] = {p: [] for p in (
            "request_prepare","request_sent","response_headers","redirect",
            "should_stream","stream_start","stream_complete",
            "response_done","error","finalize"
        )}
        # install defaults
        self._install_default_hooks()

    # public properties ----------------------
    @property
    def main_body_bytes(self):
        return self.state.main_body_bytes

    @property
    def redirect_chain(self):
        return list(self.state.redirect_chain)

    # hook registration ---------------------
    def add_hook(self, phase: str, fn: Hook):
        """register hook (wrap sync)"""
        if phase not in self._hooks:
            raise ValueError(f"unknown phase: {phase}")
        # normalize sync -> async
        if not callable(fn):
            raise TypeError("hook must be callable")
        if not hasattr(fn, "__await__"):
            async def _wrap(*a, **k):
                return fn(*a, **k)
            self._hooks[phase].append(_wrap)
        else:
            self._hooks[phase].append(fn)  # already awaitable

    # defaults -------------------------------
    def _install_default_hooks(self):
        """register stock hooks so handler works out of the box"""
        self.add_hook("request_prepare", self._default_referer_injector)
        self.add_hook("should_stream", self._default_should_stream)
        self.add_hook("stream_complete", self._default_store_stream)

    # default hook impls ---------------------
    async def _default_referer_injector(self, 
        headers: dict[str,str], 
        ev: cdp.fetch.RequestPaused, 
        state: RequestInterceptionState
    ):
        """inject referer once so servers with strict origin checks behave"""
        if not self.config.set_referer:
            return
        if state.main_url_initial is None:
            state.main_url_initial = ev.request.url
            state.main_url_current = ev.request.url
        if "referer" in headers:
            return
        try:
            scheme_split = state.main_url_initial.split("://",1)
            if len(scheme_split)==2:
                scheme, rest = scheme_split
                host = rest.split("/",1)[0]
                headers["referer"] = f"{scheme}://{host}"
        except Exception:
            logger.debug("failed to derive referer for %s", state.main_url_initial)

    async def _default_should_stream(self, 
        ev: cdp.fetch.RequestPaused, 
        mime: str, 
        state: RequestInterceptionState
    ):
        """avoid grabbing large text docs; limit to non-text main nav bodies"""
        if not self.config.capture_main_body:
            return False
        if state.main_body_done:
            return False
        if state.main_url_current and ev.request.url != state.main_url_current:
            return False
        # treat as text if any token appears
        lowered = mime.lower()
        if any(t in lowered for t in self.config.text_tokens):
            return False
        return True

    async def _default_store_stream(self, 
        body: bytes, 
        ev: cdp.fetch.RequestPaused, 
        mime: str, 
        state: RequestInterceptionState
    ):
        """persist captured body so caller can read after interception completes"""
        state.main_body_bytes = body
        state.streamed_mime = mime

    # core handler -----------------------------------
    async def handle(self, connection: nodriver.Connection, ev: cdp.fetch.RequestPaused):
        """entry point used by `connection.add_handler`"""
        # enable fetch domain once
        if not self._fetch_enabled:
            await connection.send(cdp.fetch.enable())
            self._fetch_enabled = True

        try:
            if ev.response_status_code is None:
                await self._phase_request(connection, ev)
            else:
                await self._phase_response(connection, ev)
        except Exception as exc:  # funnel to error hooks, then re-raise
            await self._run_hooks("error", exc, ev, self.state)
            raise
        finally:
            await self._run_hooks("finalize", self.state)

    # request phase ----------------------------------
    async def _phase_request(self, connection: nodriver.Connection, ev: cdp.fetch.RequestPaused):
        """prepare + continue request early so response interception can fire"""
        headers = {k.lower(): v for k,v in (ev.request.headers or {}).items()}
        headers.pop("range", None)
        await self._run_hooks("request_prepare", headers, ev, self.state)
        is_main = (self.state.main_url_current or ev.request.url) == ev.request.url
        self.state.requests[ev.request_id] = {"url": ev.request.url, "headers": headers, "main": is_main}
        await connection.send(cdp.fetch.continue_request(
            ev.request_id,
            headers=[cdp.fetch.HeaderEntry(name=k, value=v) for k,v in headers.items()],
            intercept_response=True,
        ))
        await self._run_hooks("request_sent", ev, self.state)

    # response phase ---------------------------------
    async def _phase_response(self, connection: nodriver.Connection, ev: cdp.fetch.RequestPaused):
        """handle redirect chain or stream final body; short-circuit after capture"""

        response_headers = {h.name.lower(): h.value for h in ev.response_headers}
        mime = response_headers.get("content-type", "").split(";",1)[0].strip().lower()
        await self._run_hooks("response_headers", ev, mime, response_headers, self.state)

        # redirect handling
        if self._is_redirect(ev):
            await self._handle_redirect(connection, ev, response_headers)
            return

        # decide streaming
        should_stream = await self._run_first_bool("should_stream", ev, mime, self.state)
        if not should_stream:
            await self._continue_response(connection, ev)
            await self._run_hooks("response_done", ev, mime, self.state)
            return

        # stream main body
        await self._run_hooks("stream_start", ev, mime, self.state)
        body = await self._stream_body(connection, ev, response_headers)
        self.state.main_body_done = True
        self.state.streamed_mime = mime
        await self._run_hooks("stream_complete", body, ev, mime, self.state)
        await self._run_hooks("response_done", ev, mime, self.state)

    # helpers -----------------------------------
    async def _continue_response(self, connection: nodriver.Connection, ev: cdp.fetch.RequestPaused):
        """release non-streamed interception so chrome can proceed"""
        try:
            await connection.send(cdp.fetch.continue_response(ev.request_id))
        except Exception as exc:
            if not self._is_benign(exc):
                logger.exception("continue_response error for %s:", ev.request.url)

    def _is_redirect(self, ev: cdp.fetch.RequestPaused) -> bool:
        """gate redirect logic to main nav so we don't chase subresource redirects"""
        return bool(
            ev.response_status_code 
            and 300 <= ev.response_status_code < 400 
            and self._is_main_request(ev)
        )

    def _is_main_request(self, ev: cdp.fetch.RequestPaused) -> bool:
        """identify evolving main nav across redirects"""
        return (self.state.main_url_current or ev.request.url) == ev.request.url

    async def _handle_redirect(self, 
        connection: nodriver.Connection, 
        ev: cdp.fetch.RequestPaused, 
        headers: dict[str,str]
    ):
        """update redirect chain and continue without streaming interim body"""
        loc = headers.get("location")
        old = self.state.main_url_current or ev.request.url
        if loc:
            self.state.redirect_chain.append(loc)
            self.state.main_url_current = loc
            if len(self.state.redirect_chain) > self.config.max_redirects:
                raise RuntimeError(f"redirect limit exceeded ({self.config.max_redirects}) for {old}")
            await self._run_hooks("redirect", old, loc, ev, self.state)
        await self._continue_response(connection, ev)

    async def _stream_body(self, 
        connection: nodriver.Connection, 
        ev: cdp.fetch.RequestPaused, 
        response_headers: dict[str,str]
    ):
        """capture main nav body before chrome consumes it then replay"""
        stream = await connection.send(cdp.fetch.take_response_body_as_stream(ev.request_id))
        buf = bytearray()
        while True:
            b64, data, eof = await connection.send(cdp.io.read(handle=stream))
            buf.extend(base64.b64decode(data) if b64 else data.encode())
            if eof: break
        await connection.send(cdp.io.close(handle=stream))

        # assemble ranges when server sends content-range
        cr = response_headers.get("content-range")
        if cr:
            m = RE_CONTENT_RANGE.match(cr)
            if m:
                start = int(m.group(1)); end = int(m.group(2)); total = int(m.group(3))
                self.state.range_chunks.append((start, bytes(buf)))
                if self.state.range_total is None:
                    self.state.range_total = total
                # if accumulated length >= total attempt assembly
                acc = sum(len(c) for _, c in self.state.range_chunks)
                if acc >= self.state.range_total:
                    assembled = bytearray(self.state.range_total)
                    for s, chunk in sorted(self.state.range_chunks, key=lambda x: x[0]):
                        assembled[s:s+len(chunk)] = chunk
                    body_bytes = bytes(assembled)
                else:
                    body_bytes = bytes(buf)
            else:
                body_bytes = bytes(buf)
        else:
            body_bytes = bytes(buf)

        # fulfill so page still receives body
        try:
            await connection.send(cdp.fetch.fulfill_request(
                ev.request_id,
                response_code=ev.response_status_code,
                response_headers=ev.response_headers,
                body=base64.b64encode(body_bytes).decode(),
                response_phrase=ev.response_status_text if ev.response_status_text else None,
            ))
        except Exception as exc:
            if not self._is_benign(exc):
                logger.exception("fulfill_request error for %s:", ev.request.url)
        self.state.main_body_bytes = body_bytes
        return body_bytes

    def _is_benign(self, exc: Exception) -> bool:
        """filter protocol races we expect during rapid navigation/close"""
        msg = str(exc)
        return isinstance(exc, nodriver.ProtocolException) and (
            "Invalid InterceptionId" in msg or "Inspected target navigated or closed" in msg or "Invalid state" in msg
        )

    # hook execution
    async def _run_hooks(self, phase: str, *args):
        """run all hooks; keep going even if one fails"""
        for fn in self._hooks.get(phase, ()):
            try:
                await fn(*args)
            except Exception:
                logger.exception("hook error in phase %s", phase)

    async def _run_first_bool(self, phase: str, *args) -> bool:
        """or over bool results so any truthy hook opts-in"""
        result: Optional[bool] = None
        for fn in self._hooks.get(phase, ( )):
            try:
                val = await fn(*args)
                if isinstance(val, bool):
                    result = val if result is None else (result or val)
            except Exception:
                logger.exception("hook error in phase %s", phase)
        return bool(result)
