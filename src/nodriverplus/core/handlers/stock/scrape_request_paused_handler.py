"""
# TODO:

figure out why intercepting the original request triggers cloudflare
and prevents you from receiving a `cf_clearance` token.

maybe it has to do with `Network.requestWillBeSentExtraInfo` having
extra headers that aren't provided by `Network.RequestPaused`?
"""

import logging
import nodriver

from ..request_paused import RequestPausedHandler
from ...scrape_result import ScrapeResult, InterceptedResponseMeta, InterceptedRequestMeta
import base64

logger = logging.getLogger("nodriverplus.ScrapeRequestPausedHandler")


class ScrapeRequestPausedHandler(RequestPausedHandler):
    """stock `RequestPausedHandler` created for `NodriverPlus.scrape()`

    mutates `ScrapeResult` during request/response interception.

    also mutates `ScrapeResult.bytes_` according to `should_take_response_body_as_stream()`.
    """

    def __init__(self, 
        tab: nodriver.Tab, 
        result: ScrapeResult, 
        url: str,
        scrape_bytes: bool = True
    ):
        super().__init__(tab)
        self.result = result
        self.url = url
        self.scrape_bytes = scrape_bytes


    async def on_response(self, ev):
        result = self.result
        headers = {h.name.lower(): h.value for h in (ev.response_headers or [])}
        mime = None
        ct = headers.get("content-type", "")
        if ct:
            mime = ct.split(";", 1)[0].lower().strip()

        if ev.request.url == self.url:
            result.headers = headers
            result.mime = mime
        
        result.intercepted_responses[ev.request.url] = InterceptedResponseMeta(
            ev.request.url,
            mime,
            headers,
            ev.request.method
        )
        self.result = result


    async def on_request(self, ev):
        headers = ev.request.headers.to_json()
        self.result.intercepted_requests[ev.request.url] = InterceptedRequestMeta(
            ev.request.url,
            headers,
            ev.request.method
        )


    async def should_intercept_response(self, ev):
        return ev.request.url == self.url


    async def should_take_response_body_as_stream(self, ev):
        text_types = { "text", "javascript", "json", "xml" }
        headers = {k.lower(): v for k, v in ev.request.headers.items()}
        ct = headers.get("content-type", "").lower()
        if (
            ev.request.url == self.url
            and not any(t in ct for t in text_types)
            and self.scrape_bytes
        ):
            return True
        return False
    

    async def on_stream_finished(self, ev):
        self.result.bytes_ = base64.b64decode(ev.body)


# keep this disabled until the request interception issues are resolved.
__all__ = []