import logging
import asyncio
import json
import nodriver
import time
import random
from datetime import timedelta, datetime, UTC
from nodriver import cdp
from urllib.parse import urlparse
from ..utils import fix_url, extract_links
from ..js.load import load_text as load_js
from .scrape_response import (
    ScrapeResponse, 
    ScrapeResponseHandler, 
    CrawlResult,
    CrawlResultHandler, 
    FailedLink
)
from .pause_handlers import ScrapeRequestPausedHandler
from .user_agent import UserAgent
from . import cloudflare
from .browser import get, get_with_timeout

logger = logging.getLogger(__name__)

    
async def wait_for_page_load(tab: nodriver.Tab, extra_wait_ms: int = 0):
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


async def get_user_agent(tab: nodriver.Tab):
    """evaluate js/get_user_agent.js in a tab to extract structured user agent data.

    converts returned json into the UserAgent model.

    :param tab: target tab for the operation.
    :return: structured user agent data.
    :rtype: UserAgent
    """
    js = load_js("get_user_agent.js")
    ua_data: dict = json.loads(await tab.evaluate(js, await_promise=True))
    user_agent = UserAgent.from_json(ua_data)
    logger.info("successfully retrieved user agent from %s", tab.url)
    logger.debug(
        "user agent data retrieved from %s:\n%s", tab.url, ua_data
    )
    return user_agent


async def crawl(
    base: nodriver.Tab | nodriver.Browser,
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
    proxy_server: str = None,
    proxy_bypass_list: list[str] = None,
    origins_with_universal_network_access: list[str] = None,
):
    """customizable crawl API starting at `url` up to `depth`.

    schedules scrape tasks with a worker pool and collects response metadata, errors,
    links, and timing.

    if `crawl_result_handler` is specified, `crawl_result_handler.handle()` will 
    be called and awaited before returning the final `CrawlResult`.

    `crawl_result_handler` is nifty if you're crawling with a `Manager`
    instance.

    - **`proxy_server`** — (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    - **`proxy_bypass_list`** — (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    - **`origins_with_universal_network_access`** — (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.

    :param base: target tab or browser instance to run crawl on
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
    :param proxy_server: (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    :param proxy_bypass_list: (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    :param origins_with_universal_network_access: (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.
    :return: crawl summary
    :rtype: CrawlResult
    """
    if scrape_response_handler is None:
        if not collect_responses:
            logger.warning("no `ScrapeResponseHandler` provided and collect_responses is False, only errors and links will be captured")
        else:
            logger.info("no `ScrapeResponseHandler` provided, using default handler functions")
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

    if new_window:
        # create a dedicated browser context + window; tabs we spawn will stay in this context
        instance = await get(base, new_window=True, new_context=True)

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
                scrape_response = await scrape(
                    base=instance,
                    url=current_url,
                    scrape_bytes=scrape_bytes,
                    scrape_response_handler=scrape_response_handler,
                    navigation_timeout=navigation_timeout,
                    wait_for_page_load=wait_for_page_load,
                    page_load_timeout=page_load_timeout,
                    extra_wait_ms=extra_wait_ms,
                    new_tab=True,
                    request_paused_handler=request_paused_handler,
                    proxy_server=proxy_server,
                    proxy_bypass_list=proxy_bypass_list,
                    origins_with_universal_network_access=origins_with_universal_network_access,
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


async def scrape( 
    base: nodriver.Tab | nodriver.Browser,
    url: str,
    scrape_bytes = True,
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
    proxy_server: str = None,
    proxy_bypass_list: list[str] = None,
    origins_with_universal_network_access: list[str] = None,
):
    """single page scrape (html + optional bytes + link extraction).

    handles navigation, timeouts, fetch interception, html capture, link parsing and
    cleanup (tab closure, pending task draining).

    if `scrape_response_handler` is provided, `scrape_response_handler.handle()` will
    be called and awaited before returning the final `ScrapeResponse`.

    `scrape_response_handler` could be useful if you want to execute stuff on `tab`
    after the page loads, but before the `RequestPausedHandler` is removed

    - **`proxy_server`** — (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    - **`proxy_bypass_list`** — (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    - **`origins_with_universal_network_access`** — (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.

    :param base: reuse provided tab/browser root or create fresh.
    :param url: target url.
    :param scrape_bytes: capture non-text body bytes.
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
    :param proxy_server: (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    :param proxy_bypass_list: (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    :param origins_with_universal_network_access: (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.
    :return: html/links/bytes/metadata
    :rtype: ScrapeResponse
    """
    
    start = time.monotonic()
    url = fix_url(url)

    scrape_response = ScrapeResponse(url)
    parsed_url = urlparse(url)

    # central acquisition
    tab = await get(
        base, 
        new_window=new_window, 
        new_tab=new_tab,
        proxy_server=proxy_server,
        proxy_bypass_list=proxy_bypass_list,
        origins_with_universal_network_access=origins_with_universal_network_access,
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
    # await request_paused_handler.start()

    try:
        nav_response = await get_with_timeout(
            tab,
            url,
            navigation_timeout=navigation_timeout,
            wait_for_page_load_=wait_for_page_load,
            page_load_timeout=page_load_timeout,
            extra_wait_ms=extra_wait_ms,
        )
        scrape_response.timed_out = nav_response.timed_out
        scrape_response.timed_out_navigating = nav_response.timed_out_navigating
        scrape_response.timed_out_loading = nav_response.timed_out_loading

        if not nav_response.timed_out_navigating:
            # if it's taking forever to load, get_content() will also take forever to load
            scrape_response.html = await scrape_response.tab.evaluate("document.documentElement.outerHTML")
            # use the final URL as the base for extracting links
            scrape_response.links = extract_links(scrape_response.html, scrape_response.url)
            if cloudflare.should_wait(scrape_response.html):
                logger.info("detected potentially interactable cloudflare challenge in %s", url)
            scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
            elapsed_seconds = scrape_response.elapsed.total_seconds()
            logger.info("successfully finished scrape for %s (elapsed=%.2fs)", url, elapsed_seconds)
        else:
            scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
    except Exception:
        scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
        elapsed_seconds = scrape_response.elapsed.total_seconds()
        logger.exception(
            "unexpected error during scrape for %s (elapsed=%.2fs):", url, elapsed_seconds
        )
    finally:
        # run handler + teardown before possibly closing the tab
        if scrape_response_handler:
            try:
                await scrape_response_handler.handle(scrape_response)
            except Exception:
                logger.exception("error running scrape_response_handler for %s", url)
        await request_paused_handler.stop()

    return scrape_response