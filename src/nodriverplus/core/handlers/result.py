import nodriver
from datetime import datetime, timedelta

"""lightweight data containers + handler hooks for scrape + crawl lifecycle.

mirrors style in `nodriverplus` core: simple models (responses, requests, crawl results)
and pluggable handler classes that callers can override / monkeypatch.

**TODO**:
- fix bytes streaming (currently disabled due to cloudflare issues)
"""

class InterceptedResponseMeta:
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

class InterceptedRequestMeta:
    """captured request metadata during fetch interception.

    complements InterceptedResponseMeta for debugging / audit.

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

class ScrapeResult:
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
    :param mime: mime of main response.
    :param headers: main response headers (lowercased keys).
    :param intercepted_responses: map of url->InterceptedResponseMeta.
    :param intercepted_requests: map of url->InterceptedRequestMeta.
    :param redirect_chain: ordered list of redirect target locations (raw values from Location headers).
    :param elapsed: timedelta duration of the scrape.
    :param current_depth: mostly used by `crawl()` to pass on to handlers:
    depth of this scrape in the overall crawl (0 = seed).
    """
    # bytes_ is disabled due to cloudflare+request interception issues.
    # :param `bytes_`: raw body bytes (only for non-text types when streamed).
    url: str
    tab: nodriver.Tab
    timed_out: bool
    timed_out_navigating: bool
    timed_out_loading: bool
    html: str
    # bytes_: bytes
    # avoid attribute errors for handlers expecting .links
    links: list[str] = []
    mime: str
    headers: dict
    # disabled for now due to cloudflare+request interception issues.
    # intercepted_responses: dict[str, InterceptedResponseMeta]
    # intercepted_requests: dict[str, InterceptedRequestMeta]
    redirect_chain: list[str]
    elapsed: timedelta = timedelta(0)
    current_depth: int = 0

    def __init__(self, 
        url: str = None, 
        tab: nodriver.Tab = None, 
        timed_out: bool = False, 
        timed_out_navigating: bool = False,
        timed_out_loading: bool = False,
        html: str = None, 
        # bytes_: bytes = None, 
        mime: str = None, 
        headers: dict = None, 
        # intercepted_responses: dict[str, InterceptedResponseMeta] = None,
        # intercepted_requests: dict[str, InterceptedRequestMeta] = None,
        redirect_chain: list[str] = None,
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
        :param mime: mime of main response.
        :param headers: main response headers (lowercased keys).
        :param redirect_chain: ordered list of redirect target locations (raw values from Location headers).
        :param elapsed: timedelta duration of the scrape.
        :param current_depth: mostly used by `crawl()` to pass on to handlers:
        depth of this scrape in the overall crawl (0 = seed).
        """
        # :param `bytes_`: raw body bytes (only for non-text types when streamed).
        # :param intercepted_responses: map of url->InterceptedResponseMeta.
        # :param intercepted_requests: map of url->InterceptedRequestMeta.

        # if intercepted_responses is None:
        #     intercepted_responses = {}
        # if intercepted_requests is None:
        #     intercepted_requests = {}
        if redirect_chain is None:
            redirect_chain = []

        self.url = url
        self.tab = tab
        self.timed_out = timed_out
        self.timed_out_navigating = timed_out_navigating
        self.timed_out_loading = timed_out_loading
        self.html = html
        # self.bytes_ = bytes_
        self.mime = mime
        self.headers = headers
        # self.intercepted_responses = intercepted_responses
        # self.intercepted_requests = intercepted_requests
        self.redirect_chain = redirect_chain
        self.elapsed = elapsed
        self.current_depth = current_depth


class ScrapeResultHandler:
    """pluggable hooks invoked during scrape handling inside crawl.

    override or pass callables into __init__ to customize behavior (html parsing, bytes processing,
    timeout handling, link extraction). all overridden methods must be async (unless `handle` is
    overridden to call sync methods).

    `handle()` must be async.

    `result` is mutable, so beware.
    """

    async def timed_out(self, result: ScrapeResult) -> list[str] | None:
        """called when the scrape timed out early (navigation phase).

        return list of links (rare) or None to skip expansion.

        :param result: partial result (likely missing html/bytes). (mutable)
        :return: optional new links.
        """
        pass


    async def html(self, result: ScrapeResult):
        """process populated html (and metadata) when navigation succeeded.

        mutate `result` as needed (parse, annotate, store state).

        :param result: result object. (mutable)
        """
        pass


    # disabled due to cloudflare+request interception issues.
    # async def bytes_(self, result: ScrapeResult):
    #     """process raw bytes when non-text main response was streamed.

    #     :param result: result object. (mutable)
    #     """
    #     pass


    async def links(self, result: ScrapeResult) -> list[str] | None:
        """return list of links for crawler expansion.

        default: existing extracted links.

        :param result: result object. (mutable)
        :return: list of links to continue crawl with.
        """
        return result.links


    async def handle(self, result: ScrapeResult) -> list[str] | None:
        """main entry point for handling a scrape result.

        correctly executes each step in order:

        `timed_out()`

        or: `html()` -> `bytes_()` -> `links()`
        
        **— currently,** `bytes_()` is disabled due to
        cloudflare+request interception issues.

        :param result: scrape result object (mutable).
        :return: list of links to continue crawl with.
        :rtype: list[str] | None
        """
        # if the response timed out, break.
        # (or continue I guess)
        if result.timed_out:
                return await self.timed_out(result)

        # process the response
        await self.html(result)
        # if response.bytes_:
        #     await self.bytes_(response)

        # return the links for the crawler to follow
        return await self.links(result)
    

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

    aggregates discovered + successful + failed + timed-out
    links, timing, and optionally captured ScrapeResult objects.

    :param links: all discovered links (including successful + failures).
    :param successful_links: subset successfully scraped.
    :param failed_links: list of failed link records.
    :param timed_out_links: subset that timed out navigating.
    :param time_start: crawl start timestamp (UTC).
    :param time_end: crawl end timestamp (UTC).
    :param time_elapsed: total duration.
    :param responses: optional list of every ScrapeResult captured.
    """
    links: list[str]
    successful_links: list[str]
    failed_links: list[FailedLink]
    timed_out_links: list[FailedLink]
    time_start: datetime | None
    time_end: datetime | None
    time_elapsed: timedelta | None
    responses: list[ScrapeResult] | None

    def __init__(self,
        links: list[str] = [],
        successful_links: list[str] = [],
        failed_links: list[FailedLink] = [],
        timed_out_links: list[FailedLink] = [],
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        time_elapsed: timedelta | None = None,
        responses: list[ScrapeResult] | None = None
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
