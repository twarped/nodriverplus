import asyncio
from typing import Callable, Awaitable
import nodriver
from datetime import datetime, timedelta

"""lightweight data containers + handler hooks for scrape + crawl lifecycle.

mirrors style in `nodriverplus` core: simple models (responses, requests, crawl results)
and pluggable handler classes that callers can override / monkeypatch.
"""

class ScrapeResponseIntercepted:
    """captured response metadata during fetch interception.

    populated when bytes streaming is enabled; omits body to save memory.

    :param url: response url.
    :param mime: mime type (lowercased, sans params).
    :param headers: response headers (lowercased keys).
    :param method: original request method.
    """
    url: str
    mime: str
    headers: dict
    method: str
    def __init__(self, url: str, mime: str, headers: dict, method: str):
        self.url = url
        self.mime = mime
        self.headers = headers
        self.method = method

class ScrapeRequestIntercepted:
    """captured request metadata during fetch interception.

    complements ScrapeResponseIntercepted for debugging / audit.

    :param url: request url.
    :param headers: outgoing headers
    :param method: http method.
    """
    url: str
    headers: dict
    method: str
    def __init__(self, url: str, headers: dict, method: str):
        self.url = url
        self.headers = headers
        self.method = method

class ScrapeResponse:
    """primary result object for a single page scrape.

    holds html, optional raw bytes (for non-text main resource), link list, timing + timeout flags,
    headers + mime, redirect chain, and per-request/response interception metadata.

    fields are intentionally public and lightly typed for handler mutation.

    :param url: final (or initial) url of the page.
    :param tab: underlying nodriver.Tab used for the scrape.
    :param timed_out: overall timeout flag (true if any phase timed out initially).
    :param timed_out_navigating: navigation phase timed out.
    :param timed_out_loading: load event phase timed out.
    :param html: captured html ("" when load timeout + outerHTML fallback failed).
    :param `bytes_`: raw body bytes (only for non-text types when streamed).
    :param mime: mime of main response.
    :param headers: main response headers (lowercased keys).
    :param intercepted_responses: map of url->ScrapeResponseIntercepted.
    :param intercepted_requests: map of url->ScrapeRequestIntercepted.
    :param redirect_chain: ordered list of redirect target locations (raw values from Location headers).
    :param elapsed: timedelta duration of the scrape.
    :param current_depth: mostly used by `crawl()` to pass on to handlers:
    depth of this scrape in the overall crawl (0 = seed).
    """
    url: str
    tab: nodriver.Tab
    timed_out: bool
    timed_out_navigating: bool
    timed_out_loading: bool
    html: str
    bytes_: bytes
    # avoid attribute errors for handlers expecting .links
    links: list[str] = []
    mime: str
    headers: dict
    intercepted_responses: dict[str, ScrapeResponseIntercepted]
    intercepted_requests: dict[str, ScrapeRequestIntercepted]
    redirect_chain: list[str]
    elapsed: timedelta | None
    current_depth: int = 0
    def __init__(self, 
        url: str = None, 
        tab: nodriver.Tab = None, 
        timed_out: bool = False, 
        timed_out_navigating: bool = False,
        timed_out_loading: bool = False,
        html: str = None, 
        bytes_: bytes = None, 
        mime: str = None, 
        headers: dict = None, 
        intercepted_responses: dict[str, ScrapeResponseIntercepted] = {},
        intercepted_requests: dict[str, ScrapeRequestIntercepted] = {},
        redirect_chain: list[str] = [],
        elapsed: timedelta | None = None,
        current_depth: int = 0
    ):
        """
        :param url: final (or initial) url of the page.
        :param tab: underlying nodriver.Tab used for the scrape.
        :param timed_out: overall timeout flag (true if any phase timed out initially).
        :param timed_out_navigating: navigation phase timed out.
        :param timed_out_loading: load event phase timed out.
        :param html: captured html ("" when load timeout + outerHTML fallback failed).
        :param `bytes_`: raw body bytes (only for non-text types when streamed).
        :param mime: mime of main response.
        :param headers: main response headers (lowercased keys).
        :param intercepted_responses: map of url->ScrapeResponseIntercepted.
        :param intercepted_requests: map of url->ScrapeRequestIntercepted.
        :param redirect_chain: ordered list of redirect target locations (raw values from Location headers).
        :param elapsed: timedelta duration of the scrape.
        :param current_depth: mostly used by `crawl()` to pass on to handlers:
        depth of this scrape in the overall crawl (0 = seed).
        """

        self.url = url
        self.tab = tab
        self.timed_out = timed_out
        self.timed_out_navigating = timed_out_navigating
        self.timed_out_loading = timed_out_loading
        self.html = html
        self.bytes_ = bytes_
        self.mime = mime
        self.headers = headers
        self.intercepted_responses = intercepted_responses
        self.intercepted_requests = intercepted_requests
        self.redirect_chain = redirect_chain
        self.elapsed = elapsed
        self.current_depth = current_depth


class ScrapeResponseHandler:
    """pluggable hooks invoked during scrape handling inside crawl.

    override or pass callables into __init__ to customize behavior (html parsing, bytes processing,
    timeout handling, link extraction). all main methods must be async.

    `scrape_response` is mutable, so beware.
    """

    async def timed_out(self, scrape_response: ScrapeResponse) -> list[str] | None:
        """called when the scrape timed out early (navigation phase).

        return list of links (rare) or None to skip expansion.

        :param scrape_response: partial response (likely missing html/bytes). (mutable)
        :return: optional new links.
        """
        pass


    async def html(self, scrape_response: ScrapeResponse):
        """process populated html (and metadata) when navigation succeeded.

        mutate scrape_response as needed (parse, annotate, store state).

        :param scrape_response: response object. (mutable)
        """
        pass


    async def bytes_(self, scrape_response: ScrapeResponse):
        """process raw bytes when non-text main response was streamed.

        :param scrape_response: response object. (mutable)
        """
        pass


    async def links(self, scrape_response: ScrapeResponse) -> list[str] | None:
        """return list of links for crawler expansion.

        default: existing extracted links.

        :param scrape_response: response object. (mutable)
        :return: list of links to continue crawl with.
        """
        return scrape_response.links
    

    async def handle(self, scrape_response: ScrapeResponse) -> list[str] | None:
        """main entry point for handling a scrape response.

        correctly executes each step in order:

        `timed_out()`

        or: `html()` -> `bytes_()` -> `links()`

        :param scrape_response: scrape response object (mutable).
        :return: list of links to continue crawl with.
        :rtype: list[str] | None
        """
        # if the response timed out, break.
        # (or continue I guess)
        if scrape_response.timed_out:
                return await self.timed_out(scrape_response)

        # process the response
        await self.html(scrape_response)
        if scrape_response.bytes_:
            await self.bytes_(scrape_response)

        # return the links for the crawler to follow
        return await self.links(scrape_response)
    

class FailedLink:
    """record of a link that failed or timed out during crawl.

    :param url: target link.
    :param timed_out: navigation timeout flag.
    :param error: captured exception (if any).
    """
    url: str
    timed_out: bool
    error: Exception

    def __init__(self, url: str, timed_out: bool, error: Exception):
        self.url = url
        self.timed_out = timed_out
        self.error = error

class CrawlResult:
    """final summary of a crawl run.

    aggregates discovered + successful + failed + timed-out links, timing, and optionally captured ScrapeResponse objects.

    :param links: all discovered links (including successful + failures).
    :param successful_links: subset successfully scraped.
    :param failed_links: list of failed link records.
    :param timed_out_links: subset that timed out navigating.
    :param time_start: crawl start timestamp (UTC).
    :param time_end: crawl end timestamp (UTC).
    :param time_elapsed: total duration.
    :param responses: optional list of every ScrapeResponse captured.
    """
    links: list[str]
    successful_links: list[str]
    failed_links: list[FailedLink]
    timed_out_links: list[FailedLink]
    time_start: datetime | None
    time_end: datetime | None
    time_elapsed: timedelta | None
    responses: list[ScrapeResponse] | None

    def __init__(self,
        links: list[str] = [],
        successful_links: list[str] = [],
        failed_links: list[FailedLink] = [],
        timed_out_links: list[FailedLink] = [],
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        time_elapsed: timedelta | None = None,
        responses: list[ScrapeResponse] | None = None
    ):
        self.links = links
        self.successful_links = successful_links
        self.failed_links = failed_links
        self.timed_out_links = timed_out_links
        self.time_start = time_start
        self.time_end = time_end
        self.time_elapsed = time_elapsed
        self.responses = responses

class CrawlResultHandler:
    """custom hook used by `Manager` when running a crawl

    supply a callable to __init__ or subclass and override `handle`.
    """

    async def handle(self, result: CrawlResult):
        """final hook for when a crawl completes.

        `result` is mutable and is returned by `crawl()`

        :param result: crawl summary.
        """
        pass
