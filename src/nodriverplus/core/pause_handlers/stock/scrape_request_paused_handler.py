import logging
import nodriver

from ..requests import RequestPausedHandler
from ...scrape_response import ScrapeResponse, ScrapeResponseIntercepted, ScrapeRequestIntercepted
import base64

logger = logging.getLogger(__name__)


class ScrapeRequestPausedHandler(RequestPausedHandler):
    """stock `RequestPausedHandler` created for `NodriverPlus.scrape()`

    mutates `ScrapeResponse` during request/response interception.

    also mutates `ScrapeResponse.bytes_` according to `should_take_response_body_as_stream()`.
    """

    def __init__(self, 
        tab: nodriver.Tab, 
        scrape_response: ScrapeResponse, 
        url: str,
        scrape_bytes: bool = True
    ):
        super().__init__(tab)
        self.scrape_response = scrape_response
        self.url = url
        self.scrape_bytes = scrape_bytes


    async def on_response(self, ev):
        scrape_response = self.scrape_response
        headers = {h.name.lower(): h.value for h in (ev.response_headers or [])}
        mime = None
        ct = headers.get("content-type", "")
        if ct:
            mime = ct.split(";", 1)[0].lower().strip()

        if ev.request.url == self.url:
            scrape_response.headers = headers
            scrape_response.mime = mime
        
        scrape_response.intercepted_responses[ev.request.url] = ScrapeResponseIntercepted(
            ev.request.url,
            mime,
            headers,
            ev.request.method
        )
        self.scrape_response = scrape_response


    async def on_request(self, ev):
        headers = ev.request.headers.to_json()
        self.scrape_response.intercepted_requests[ev.request.url] = ScrapeRequestIntercepted(
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
        self.scrape_response.bytes_ = base64.b64decode(ev.body)
