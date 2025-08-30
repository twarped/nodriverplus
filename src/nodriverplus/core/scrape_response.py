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
    :param headers: outgoing headers (lowercased keys; range removed).
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
    :param bytes_: raw body bytes (only for non-text types when streamed).
    :param mime: mime of main response.
    :param headers: main response headers (lowercased keys).
    :param intercepted_responses: map of url->ScrapeResponseIntercepted.
    :param intercepted_requests: map of url->ScrapeRequestIntercepted.
    :param redirect_chain: ordered list of redirect target locations (raw values from Location headers).
    :param elapsed: timedelta duration of the scrape.
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
        elapsed: timedelta | None = None
    ):
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


class ScrapeResponseHandler:
    """pluggable hooks invoked during scrape handling inside crawl.

    override or pass callables into __init__ to customize behavior (html parsing, bytes processing,
    timeout handling, link extraction). each method may be sync or async except for `handle`.
    """

    async def timed_out(self, scrape_response: ScrapeResponse) -> list[str] | None:
        """called when the scrape timed out early (navigation phase).

        return list of links (rare) or None to skip expansion.

        :param scrape_response: partial response (likely missing html/bytes).
        :return: optional new links.
        """
        pass


    async def html(self, scrape_response: ScrapeResponse):
        """process populated html (and metadata) when navigation succeeded.

        mutate scrape_response as needed (parse, annotate, store state).

        :param scrape_response: populated response object.
        """
        pass


    async def bytes_(self, scrape_response: ScrapeResponse):
        """process raw bytes when non-text main response was streamed.

        skip if bytes_ is None. can persist file, hash, etc.

        :param scrape_response: response object (bytes_ may be None).
        """
        pass


    async def links(self, scrape_response: ScrapeResponse) -> list[str]:
        """return list of links for crawler expansion.

        default: existing extracted links.

        :param scrape_response: response object.
        :return: list of links to continue crawl with.
        """
        return scrape_response.links
    

    async def handle(self, scrape_response: ScrapeResponse) -> list[str] | None:
        # if the response timed out, break.
        # (or continue I guess)
        if scrape_response.timed_out:
            if asyncio.iscoroutinefunction(self.timed_out):
                return await self.timed_out(scrape_response)
            else:
                return self.timed_out(scrape_response)

        # process the response
        if asyncio.iscoroutinefunction(self.html):
            await self.html(scrape_response)
        else:
            self.html(scrape_response)
        if scrape_response.bytes_:
            if asyncio.iscoroutinefunction(self.bytes_):
                await self.bytes_(scrape_response)
            else:
                self.bytes_(scrape_response)

        # return the links for the crawler to follow
        if asyncio.iscoroutinefunction(self.links):
            return await self.links(scrape_response)
        else:
            return self.links(scrape_response)
    

    def __init__(self,
        timed_out: Callable[[ScrapeResponse], Awaitable[None] | None] = None,
        html: Callable[[ScrapeResponse], Awaitable[None] | None] = None,
        bytes_: Callable[[ScrapeResponse], Awaitable[None] | None] = None,
        links: Callable[[ScrapeResponse], Awaitable[list[str]] | list[str]] = None,
        handle: Callable[[ScrapeResponse], Awaitable[list[str] | None] | list[str] | None] = None
    ):
        """optional injection of hook functions.

        any provided callable replaces the corresponding method 
        (supports sync or async except for `handle` which must be async).

        :param timed_out: handler for timeout case.
        :param html: handler after html captured.
        :param bytes_: handler after bytes captured.
        :type bytes_: bytes_
        :param links: link selection handler.
        :param handle: full override (advanced; skips built-in sequence). 
        **must** return a list of links for crawl to continue. (empty is the end of the tree)
        """
        if timed_out:
            self.timed_out = timed_out
        if html:
            self.html = html
        if bytes_:
            self.bytes_ = bytes_
        if links:
            self.links = links
        if handle:
            self.handle = handle

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
    """custom hook used by `NodriverPlusManager` when running a crawl

    supply a callable to __init__ or subclass and override `handle`.
    """

    async def handle(self, result: CrawlResult):
        """process finished CrawlResult (persist / summarize / metrics).

        :param result: crawl summary.
        """
        pass

    def __init__(self, handle: Callable[[CrawlResult], Awaitable[any] | None] | None = None):
        """allows for custom handling of crawl results during a queued crawl.
        (NodriverPlusManager)

        :param handle: optional custom handler for crawl results.
        """
        if handle:
            self.handle = handle
