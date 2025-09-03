"""stealth + crawl helpers layered over `nodriver`.

wraps nodriver to add:
- user agent acquisition + patch (network / emulation / runtime) with headless token scrub
- stealth scripts (navigator / plugins / workers) auto-applied via Target.setAutoAttach
- single page scrape (html + optional raw bytes + link extraction)
- crawl (depth, concurrency, jitter, per-page handler, error + timeout tracking)
- fetch interception to stream non-text main bodies (e.g. pdf) before chrome consumes them
- lightweight cloudflare challenge surface detection (caller can extend)

keeps low-level access to the underlying nodriver Browser so callers can still drive CDP directly.
definitely still needs work tho
"""
import asyncio
import time
import random
from datetime import datetime, UTC
import logging
from os import PathLike
import asyncio
from urllib.parse import urlparse
import nodriver
from nodriver import Config, cdp
from .user_agent import *
from .scrape_response import *
from ..utils import extract_links, fix_url
from . import cloudflare
from datetime import timedelta
from .tab import acquire_tab, get_user_agent
from .pause_handlers import TargetInterceptor, TargetInterceptorManager
from .pause_handlers.stock import (
    UserAgentPatch, 
    StealthPatch, 
    patch_stealth,
    patch_user_agent,
    ScrapeRequestPausedHandler,
    WindowSizePatch,
    patch_window_size,
)

logger = logging.getLogger(__name__)



class NodriverPlus:
    """high-level orchestrator for starting a stealthy browser and performing scrapes/crawls.

    lifecycle:
    1. `start()`: launch chrome, fetch + patch user agent, install stealth auto-attach
    2. `scrape()`: navigate + capture html / links / headers / optional bytes
    3. `crawl()`: customizable nodriver crawling API with depth + concurrency + handler-produced link expansion
    4. `stop()`: shutdown underlying process (optional graceful wait)

    bytes capture: uses Fetch domain interception + fulfill flow to stream non-text main bodies.
    ua patching: applies Network + Emulation overrides + runtime JS patch to sync navigator.* / userAgentData.
    stealth: early evaluate scripts for pages + workers (plugins, languages, canvas, webdriver flag, etc.).
    """
    browser: nodriver.Browser
    config: nodriver.Config
    user_agent: UserAgent
    stealth: bool
    interceptor_manager: TargetInterceptorManager

    def __init__(self, 
        user_agent: UserAgent = None, 
        stealth: bool = True, 
        interceptors: list[TargetInterceptor] = None
    ):
        """initialize a `NodriverPlus` instance
        
        :param user_agent: `UserAgent` to patch browser with 
        using stock interceptor: `UserAgentInterceptor`
        :param stealth: whether to apply the stock interceptor: `StealthInterceptor`
        :param interceptors: list of additional custom interceptors to apply
        """
        self.config = Config()
        self.browser = None
        self.user_agent = user_agent
        self.stealth = stealth
        # init interceptor manager and add provided + stock interceptors
        interceptor_manager = TargetInterceptorManager()
        interceptor_manager.interceptors.extend(interceptors or [])
        if user_agent:
            interceptor_manager.interceptors.append(UserAgentPatch(user_agent, stealth))
        if stealth:
            interceptor_manager.interceptors.append(StealthPatch())
        self.interceptor_manager = interceptor_manager


    # TODO: make a `WindowSize` dataclass that can be passed
    # so that users can specify other meta like
    # `device_scale_factor`, `mobile`, and `orientation`
    async def start(self,
        config: Config | None = None,
        *,
        window_size: tuple[int, int] | None = (1920, 1080),
        user_data_dir: PathLike | None = None,
        headless: bool | None = False,
        browser_executable_path: PathLike | None = None,
        browser_args: list[str] | None = None,
        sandbox: bool | None = True,
        lang: str | None = None,
        host: str | None = None,
        port: int | None = None,
        expert: bool | None = None,
        **kwargs: dict | None,
    ) -> nodriver.Browser:
        """launch a browser and prime stock interceptors.

        **specify *`window_size`* to apply a global window size patch.**
        - applies the correct `browser_args`
        - and applies the stock `WindowSizePatch` interceptor to the browser

        wraps nodriver.start then optionally fetches + patches the user agent and installs
        stealth scripts. returns the raw
        nodriver Browser so callers can still use low-level APIs.

        :param config: optional pre-built Config.
        :param window_size: optional window size to apply a global window size patch.
        :type window_size: pixels: (width, height)
        :param user_data_dir: chrome profile dir.
        :param headless: run in headless mode (we still scrub Headless tokens later).
        :param browser_executable_path: custom chrome path.
        :param browser_args: extra chrome flags.
        :param sandbox: disable linux sandbox when False.
        :param lang: accept-language override.
        :param host: devtools host.
        :param port: devtools port.
        :param expert: nodriver expert mode flag.
        :param kwargs: forwarded to nodriver.start.
        :return: started browser instance.
        :rtype: nodriver.Browser
        """
        if window_size:
            width, height = window_size
            # remove any existing --window-size arg to avoid dupes
            if browser_args is None:
                browser_args = []
            browser_args = [
                arg for arg in browser_args
                if not arg.startswith("--window-size=")
            ]
            browser_args.append(f"--window-size={width},{height}")
            # add interceptor to manager
            self.interceptor_manager.interceptors.append(
                WindowSizePatch(width, height)
            )

        self.browser = await nodriver.start(
            config,
            user_data_dir=user_data_dir,
            headless=headless,
            browser_executable_path=browser_executable_path,
            browser_args=browser_args,
            sandbox=sandbox,
            lang=lang,
            host=host,
            port=port,
            expert=expert,
            **kwargs
        )

        # get user agent if none specified
        if not self.user_agent:
            user_agent = await get_user_agent(self.browser.main_tab)
            self.user_agent = user_agent
        # just in case they added self.user_agent outside of `__init__()`
        if not any(isinstance(i, UserAgentPatch) for i in self.interceptor_manager.interceptors):
            self.interceptor_manager.interceptors.append(
                UserAgentPatch(self.user_agent, self.stealth)
            )

        # apply patches to main tab
        await patch_stealth(self.browser.main_tab, None)

        await patch_user_agent(
            self.browser.main_tab, 
            None,
            self.user_agent,
            self.stealth
        )

        if window_size:
            width, height = window_size
            await patch_window_size(
                self.browser.main_tab,
                None,
                width=width,
                height=height,
            )

        self.interceptor_manager.connection = self.browser.connection
        await self.interceptor_manager.start()

        self.config = self.browser.config
        return self.browser


    async def crawl(self,
        url: str,
        scrape_response_handler: ScrapeResponseHandler = None,
        depth = 1,
        crawl_result_handler: CrawlResultHandler = None,
        *,
        new_window = False,
        scrape_bytes = True,
        navigation_timeout = 30,
        wait_for_page_load = True,
        page_load_timeout = 60,
        extra_wait_ms = 0,
        concurrency: int = 1,
        max_pages: int | None = None,
        collect_responses: bool = False,
        delay_range: tuple[float, float] | None = None,
        request_paused_handler: ScrapeRequestPausedHandler = None,
    ):
        """customizable crawl API starting at `url` up to `depth`.

        schedules scrape tasks with a worker pool, collects response metadata, errors,
        links, and timing. handler is invoked for each page producing optional new links.

        if `crawl_result_handler` is specified, `crawl_result_handler.handle()` will 
        be called and awaited before returning the final `CrawlResult`.

        `crawl_result_handler` is nifty if you're crawling with a `NodriverPlusManager`
        instance.

        :param url: root starting point.
        :param scrape_response_handler: optional `ScrapeResponseHandler` to be passed to `scrape()`
        :param depth: max link depth (0 means single page).
        :param crawl_result_handler: if specified, `crawl_result_handler.handle()` 
        will be called and awaited before returning the final `CrawlResult`.
        :param new_window: isolate crawl in new context+window when True.
        :param scrape_bytes: capture bytes stream when possible.
        :param navigation_timeout: seconds for initial navigation phase.
        :param wait_for_page_load: await full load event.
        :param page_load_timeout: seconds for load phase.
        :param extra_wait_ms: post-load settle time.
        :param concurrency: worker concurrency.
        :param max_pages: hard cap on processed pages.
        :param collect_responses: store every ScrapeResponse object.
        :param delay_range: (min,max) jitter before first scrape per worker loop.
        :param tab_close_timeout: seconds to wait closing a tab.
        :param request_paused_handler: custom fetch interception handler.
        **must be a type**—not an instance—so that it can be initiated later with the correct values attached
        :type request_paused_handler: type[ScrapeRequestPausedHandler]
        :return: crawl summary
        :rtype: CrawlResult
        """
        if scrape_response_handler is None:
            if not collect_responses:
                logger.warning("no handler provided and collect_responses is False, only errors and links will be captured")
            else:
                logger.info("no handler provided, using default handler functions")
            scrape_response_handler = ScrapeResponseHandler()

        # normalize delay range if provided
        if delay_range is not None:
            a, b = delay_range
            if a > b:
                delay_range = (b, a)
            if a < 0 or b < 0:
                delay_range = None  # disallow negative
        logger.info(
            "crawl started for %s (depth=%d concurrency=%d max_pages=%s delay=%s)",
            url, depth, concurrency, max_pages, delay_range,
        )

        root_url = fix_url(url)
        depth = max(0, depth)
        concurrency = max(1, concurrency)

        time_start = datetime.now(UTC)

        # queue: (url, remaining_depth)
        queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        await queue.put((root_url, depth))

        # visited = fully processed
        # all_links_set = discovered (even if not yet processed)
        visited: set[str] = set()
        all_links: list[str] = [root_url]
        all_links_set: set[str] = {root_url}
        successful_links: list[str] = []
        failed_links: list[FailedLink] = []
        timed_out_links: list[FailedLink] = []
        # optional heavy list of every response captured
        responses: list[ScrapeResponse] = [] if collect_responses else None  # type: ignore

        pages_processed = 0
        lock = asyncio.Lock()  # protects shared collections when needed
        # map worker idx -> current url for runtime debugging
        current_processing: dict[int, str] = {}

        async def should_enqueue(link: str, remaining: int) -> bool:
            # depth 0 should just be a single scrape
            if remaining < 0:
                return False
            try:
                link_canon = fix_url(link)
            except Exception:
                return False
            if link_canon in visited or link_canon in all_links_set:
                return False
            parsed = urlparse(link_canon)
            # throw interesting links away
            if parsed.scheme not in ("http", "https"):
                return False
            async with lock:  # race-safe insertion into discovery sets
                if link_canon not in all_links_set:
                    all_links_set.add(link_canon)
                    all_links.append(link_canon)
            return True

        instance: nodriver.Tab | nodriver.Browser = self.browser
        if new_window:
            # create a dedicated browser context + window; tabs we spawn will stay in this context
            instance = await acquire_tab(self.browser, new_window=True, new_context=True)

        async def worker(idx: int):
            nonlocal pages_processed
            while True:
                try:
                    # wait for next target
                    current_url, remaining_depth = await queue.get()
                except asyncio.CancelledError:
                    break
                # register current work for debugging/monitoring
                try:
                    current_processing[idx] = current_url
                except Exception:
                    current_processing[idx] = str(current_url)
                if current_url in visited:
                    # clear current marker before finishing item
                    current_processing.pop(idx, None)
                    queue.task_done()
                    continue
                visited.add(current_url)
                if max_pages is not None and pages_processed >= max_pages:
                    current_processing.pop(idx, None)
                    queue.task_done()
                    continue
                error_obj: Exception | None = None
                scrape_response: ScrapeResponse | None = None
                try:
                    is_first_depth = remaining_depth + 1 == depth
                    # simple delay / jitter if specified; skip if first scrape
                    if delay_range is not None and is_first_depth:
                        wait_time = random.uniform(*delay_range)
                        logger.info("waiting %.2f seconds before scraping %s", wait_time, current_url)
                        await asyncio.sleep(wait_time)
                    scrape_response = await self.scrape(
                        current_url,
                        scrape_bytes,
                        instance,
                        scrape_response_handler,
                        navigation_timeout=navigation_timeout,
                        wait_for_page_load=wait_for_page_load,
                        page_load_timeout=page_load_timeout,
                        extra_wait_ms=extra_wait_ms,
                        new_tab=True,
                        request_paused_handler=request_paused_handler,
                    )
                    pages_processed += 1
                    links: list[str] = []
                    try:
                        # remember: the handler can mutate links
                        links = await scrape_response_handler.handle(scrape_response) or []
                    except Exception as e:
                        error_obj = e
                        logger.exception("failed running handler for %s:", current_url)
                    finally:
                        if scrape_response.tab:
                            # avoid closing tabs twice
                            if not (new_window and scrape_response.tab is instance):
                                # run close in its own task so a hung 
                                # protocol `Transaction` can't block join()
                                t = asyncio.create_task(scrape_response.tab.close())
                                t.add_done_callback(lambda f: logger.info("tab closed: %s", f.result()))

                    # scrape timed out or failed
                    if scrape_response.timed_out_navigating:
                        timed_out_links.append(FailedLink(current_url, scrape_response.timed_out_navigating, error_obj))
                    elif error_obj:
                        failed_links.append(FailedLink(current_url, scrape_response.timed_out_navigating, error_obj))
                    else:
                        # scrape was a success
                        # record the final URL if the page redirected
                        final_url = getattr(scrape_response, "url", None) or (scrape_response.tab.url if scrape_response.tab else current_url)
                        try:
                            final_url = fix_url(final_url)
                        except Exception:
                            final_url = final_url or current_url

                        async with lock:
                            if final_url not in all_links_set:
                                all_links_set.add(final_url)
                                all_links.append(final_url)

                        successful_links.append(final_url)

                    if collect_responses and scrape_response:
                        async with lock:
                            responses.append(scrape_response)

                    next_remaining = remaining_depth - 1
                    # depth 0 should still be allowed because depth
                    # 1 should be actually scraping with a depth
                    if next_remaining > -1 and links:
                        for link in links:
                            if max_pages is not None and pages_processed >= max_pages:
                                break
                            if await should_enqueue(link, next_remaining):
                                await queue.put((fix_url(link), next_remaining))
                except Exception as e:
                    failed_links.append(FailedLink(current_url, False, e))
                    logger.exception("unexpected error during crawl for %s", current_url)
                finally:
                    # clear current marker and mark as done so join can finish
                    current_processing.pop(idx, None)
                    queue.task_done()

        workers = [asyncio.create_task(worker(i)) for i in range(concurrency)]

        # wait until everything is finished
        await queue.join()
        for w in workers:
            w.cancel()
        # we're trying to cancel everything
        # so ignore CancelledError
        for w in workers:
            try:
                await w
            except asyncio.CancelledError:
                pass

        if new_window and isinstance(instance, nodriver.Tab):
            # close the dedicated context tab with a timeout to avoid hangs
            try:
                t = asyncio.create_task(instance.close())
                await asyncio.wait_for(t, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("timeout closing dedicated context tab (continuing)")
                t.cancel()
            except Exception:
                logger.debug("failed closing dedicated context tab (already closed?)")

        time_end = datetime.now(UTC)
        time_elapsed = time_end - time_start
        result = CrawlResult(
            links=all_links,
            successful_links=successful_links,
            failed_links=failed_links,
            timed_out_links=timed_out_links,
            time_start=time_start,
            time_end=time_end,
            time_elapsed=time_elapsed,
            responses=responses,
        )

        if crawl_result_handler:
            await crawl_result_handler.handle(result)
            result.time_end = datetime.now(UTC)
            result.time_elapsed = result.time_end - time_start

        logger.info(
            "successfully finished crawl for %s (pages=%d success=%d failed=%d timed_out=%d elapsed=%.2fs)",
            url,
            len(all_links),
            len(successful_links),
            len(failed_links),
            len(timed_out_links),
            (time_end - time_start).total_seconds(),
        )
        return result


    # TODO: refactor NodriverPlus to pull from 
    # dedicated `Tab` functions like this
    async def scrape(self, 
        url: str,
        scrape_bytes = True,
        existing_tab: nodriver.Tab | None = None,
        scrape_response_handler: ScrapeResponseHandler | None = None,
        *,
        navigation_timeout = 30,
        wait_for_page_load = True,
        page_load_timeout = 60,
        extra_wait_ms = 0,
        # solve_cloudflare = True, # not implemented yet
        new_tab = False,
        new_window = False,
        request_paused_handler = ScrapeRequestPausedHandler,
    ):
        """single page scrape (html + optional bytes + link extraction).

        handles navigation, timeouts, fetch interception, html capture, link parsing and
        cleanup (tab closure, pending task draining).

        if `scrape_response_handler` is provided, `scrape_response_handler.handle()` will
        be called and awaited before returning the final `ScrapeResponse`.

        `scrape_response_handler` could be useful if you want to execute stuff on `tab`
        after the page loads, but before the `RequestPausedHandler` is removed

        :param url: target url.
        :param scrape_bytes: capture non-text body bytes.
        :param existing_tab: reuse provided tab/browser root or create fresh.
        :param scrape_response_handler: if specified, `scrape_response_handler.handle()` will be called 
        and awaited before returning the final `ScrapeResponse`
        :param navigation_timeout: seconds for initial navigation.
        :param wait_for_page_load: await full load event.
        :param page_load_timeout: seconds for load phase.
        :param extra_wait_ms: post-load wait for dynamic content.
        :param new_tab: request new tab.
        :param new_window: request isolated window/context.
        :param request_paused_handler: custom fetch interception handler.
        **must be a type**—not an instance—so that it can be initiated later with the correct values attached
        :type request_paused_handler: type[ScrapeRequestPausedHandler]
        :return: html/links/bytes/metadata
        :rtype: ScrapeResponse
        """
        
        start = time.monotonic()
        target = existing_tab or self.browser
        url = fix_url(url)

        scrape_response = ScrapeResponse(url)
        parsed_url = urlparse(url)

        if target is None:
            raise ValueError("browser was never started! start with `NodriverPlus.start(**kwargs)`")
        # central acquisition
        tab = await acquire_tab(
            target, 
            new_window=new_window, 
            new_tab=new_tab, 
        )
        scrape_response.tab = tab
        logger.info("scraping %s", url)

        await tab.send(cdp.network.enable())
        await tab.send(
            cdp.network.set_extra_http_headers(
                headers=cdp.network.Headers({
                    "Referer": f"{parsed_url.scheme}://{parsed_url.netloc}",
                })
            )
        )

        # use the stock handler unless a custom one is provided
        request_paused_handler = (request_paused_handler or ScrapeRequestPausedHandler)(
            tab, scrape_response, url, scrape_bytes
        )
        await request_paused_handler.start()

        try:
            nav_response = await self.get_with_timeout(tab, url, 
                navigation_timeout=navigation_timeout, 
                wait_for_page_load=wait_for_page_load, 
                page_load_timeout=page_load_timeout, 
                extra_wait_ms=extra_wait_ms
            )
            scrape_response.timed_out = nav_response.timed_out
            scrape_response.timed_out_navigating = nav_response.timed_out_navigating
            scrape_response.timed_out_loading = nav_response.timed_out_loading
            if nav_response.timed_out_navigating:
                return scrape_response

            # prefer the tab's final URL (handles redirects) and record it on the ScrapeResponse
            final_url = nav_response.tab.url if getattr(nav_response, "tab", None) and nav_response.tab.url else url
            scrape_response.url = fix_url(final_url)

            # if it's taking forever to load, get_content() will also take forever to load
            if scrape_response.timed_out_loading:
                # fast path: avoid get_content() which can hang when load timed out
                val = await scrape_response.tab.evaluate("document.documentElement.outerHTML")
                if isinstance(val, cdp.runtime.ExceptionDetails):
                    logger.warning("failed evaluating outerHTML for %s after load timeout; treating as empty doc", url)
                    scrape_response.html = ""
                else:
                    scrape_response.html = val if isinstance(val, str) else str(val)
            else:
                scrape_response.html = await nav_response.tab.get_content()
            # use the final URL as the base for extracting links
            scrape_response.links = extract_links(scrape_response.html, final_url)
            if cloudflare.should_wait(scrape_response.html):
                logger.info("detected potentially interactable cloudflare challenge in %s", url)
            scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
            elapsed_seconds = scrape_response.elapsed.total_seconds()
            logger.info("successfully finished scrape for %s (elapsed=%.2fs)", url, elapsed_seconds)
        except Exception:
            scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
            elapsed_seconds = scrape_response.elapsed.total_seconds()
            logger.exception("unexpected error during scrape for %s (elapsed=%.2fs):", url, elapsed_seconds)

        if scrape_response_handler:
            await scrape_response_handler.handle(scrape_response)
        await request_paused_handler.stop()

        return scrape_response
    

    async def wait_for_page_load(self, tab: nodriver.Tab, extra_wait_ms: int = 0):
        """wait for load event (or immediate if already complete) then optional delay.

        :param tab: target tab.
        :param extra_wait_ms: additional ms sleep via setTimeout after load.
        """
        await tab.evaluate("""
            new Promise(r => {
                if (document.readyState === "complete") {
                    r();
                } else {
                    window.addEventListener("load", r);
                }
            })
        """, await_promise=True)
        logger.debug("successfully finished loading %s", tab.url)
        if extra_wait_ms:
            logger.debug("waiting extra %d ms for %s", extra_wait_ms, tab.url)
            await tab.evaluate(
                f"new Promise(r => setTimeout(r, {extra_wait_ms}));",
                await_promise=True
            )


    async def get_with_timeout(self, 
        tab: nodriver.Tab | nodriver.Browser, 
        url: str, 
        *,
        navigation_timeout = 30,
        wait_for_page_load = True,
        page_load_timeout = 60,
        extra_wait_ms = 0,
        new_tab = False,
        new_window = False
    ):
        """navigate with separate navigation + load timeouts.

        returns a partial ScrapeResponse (timing + timeout flags + tab ref)

        :param tab: existing tab or browser root.
        :param url: target url.
        :param navigation_timeout: seconds for navigation phase.
        :param wait_for_page_load: whether to wait for load event.
        :param page_load_timeout: seconds for load phase.
        :param extra_wait_ms: post-load wait for dynamic content.
        :param new_tab: request new tab first.
        :param new_window: request new window/context first.
        :return: partial ScrapeResponse (timing + timeout flags).
        :rtype: ScrapeResponse
        """
        scrape_response = ScrapeResponse(url, tab, True)

        start = time.monotonic()
        # prepare target first when we need a new tab/window/context
        base = tab
        if new_tab or new_window:
            try:
                base = await acquire_tab(
                    tab if isinstance(tab, nodriver.Tab) else self.browser,
                    "about:blank",
                    new_window=new_window,
                    new_tab=new_tab,
                    new_context=new_window,
                )
            except Exception:
                logger.exception("failed acquiring tab; falling back to provided target")
        nav_task = asyncio.create_task(base.get(url))
        try:
            # cancelling nav_task will cause throw an InvalidStateError
            # if the Transaction hasn't finished yet
            base = await asyncio.wait_for(asyncio.shield(nav_task), timeout=navigation_timeout)
            scrape_response.tab = base
        except asyncio.TimeoutError:
            scrape_response.timed_out_navigating = True
            scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
            logger.warning("timed out getting %s (navigation phase) (elapsed=%.2fs)", url, scrape_response.elapsed.total_seconds())
            return scrape_response

        if wait_for_page_load:    
            load_task = asyncio.create_task(self.wait_for_page_load(base, extra_wait_ms))
            try:
                # same thing here
                await asyncio.wait_for(
                    asyncio.shield(load_task), 
                    timeout=page_load_timeout + extra_wait_ms / 1000
                )
            except asyncio.TimeoutError:
                scrape_response.timed_out_loading = True
                scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
                logger.warning("timed out getting %s (load phase) (elapsed=%.2fs)", url, scrape_response.elapsed.total_seconds())
                # wait for task to actually cancel
                if not load_task.done():
                    load_task.cancel()
                    try:
                        await load_task
                    except asyncio.CancelledError:
                        pass
                return scrape_response

        scrape_response.timed_out = False
        scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
        return scrape_response
    

    async def stop(self, graceful = True):
        """stop browser process (optionally wait for graceful exit).

        :param graceful: wait for underlying process to exit.
        """
        logger.info("stopping browser")
        self.browser.stop()
        if graceful:
            logger.info("waiting for graceful shutdown")
            await self.browser._process.wait()
        logger.info("successfully shutdown browser")