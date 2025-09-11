"""
**TODO**:
- move request paused handlers to `TargetInterceptorManager` probably
"""

import logging
import asyncio
import json
import nodriver
import time
import random
import re
import base64
import os
from pathlib import Path
from datetime import timedelta, datetime, UTC
from nodriver import cdp
from urllib.parse import urlparse
from ..utils import fix_url, extract_links
from ..js.load import load_text as load_js
from .scrape_result import (
    ScrapeResult, 
    ScrapeResultHandler, 
    CrawlResult,
    CrawlResultHandler, 
    FailedLink
)
# from .pause_handlers import ScrapeRequestPausedHandler
from .user_agent import UserAgent
from .browser import get, get_with_timeout

logger = logging.getLogger("nodriverplus.tab")


# lazy cv2 + numpy import to keep import cost tiny when unused
try:  # slim optional dep
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except Exception:  # noqa: broad ok here (missing dep)
    cv2 = None  # type: ignore
    np = None  # type: ignore

    
async def wait_for_page_load(tab: nodriver.Tab, extra_wait_ms: int = 0):
    """wait for load event (or immediate if already complete) then optional delay.

    :param tab: target tab.
    :param extra_wait_ms: additional ms sleep via setTimeout after load.
    """
    
    # if the document is already fully loaded, return immediately (about:blank)
    if await tab.evaluate("document.readyState") == "complete":
        return

    loop = asyncio.get_running_loop()
    ev = asyncio.Event()

    def _handler(_event):
        loop.call_soon_threadsafe(ev.set)

    tab.add_handler([cdp.page.LoadEventFired], handler=_handler)
    try:
        await ev.wait()
        logger.info("successfully finished loading %s", getattr(tab, "url", "<unknown>"))
        if extra_wait_ms:
            logger.info("waiting extra %d ms for %s", extra_wait_ms, getattr(tab, "url", "<unknown>"))
            await asyncio.sleep(extra_wait_ms / 1000)
    finally:
        tab.remove_handler([cdp.page.LoadEventFired], handler=_handler)


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
        "user agent data retrieved from %s:\n%s", tab.url, json.dumps(ua_data, indent=2)
    )
    return user_agent


async def crawl(
    base: nodriver.Tab | nodriver.Browser,
    url: str,
    scrape_result_handler: ScrapeResultHandler = None,
    depth: int | None = 1,
    crawl_result_handler: CrawlResultHandler = None,
    *,
    new_window = False,
    # scrape_bytes = True,
    navigation_timeout = 30,
    wait_for_page_load = True,
    page_load_timeout = 60,
    extra_wait_ms = 0,
    concurrency: int = 1,
    max_pages: int | None = None,
    collect_results: bool = False,
    delay_range: tuple[float, float] | None = None,
    # request_paused_handler: ScrapeRequestPausedHandler = None,
    proxy_server: str = None,
    proxy_bypass_list: list[str] = None,
    origins_with_universal_network_access: list[str] = None,
):
    """customizable crawl API starting at `url` up to `depth`.

    schedules scrape tasks with a worker pool and collects result metadata, errors,
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
    :param scrape_result_handler: optional `ScrapeResultHandler` to be passed to `scrape()`
    :param depth: max link depth (0 means single page).
    :param crawl_result_handler: if specified, `crawl_result_handler.handle()` 
    will be called and awaited before returning the final `CrawlResult`.
    :param new_window: isolate crawl in new context+window when True.
    :param navigation_timeout: seconds for initial navigation phase.
    :param wait_for_page_load: await full load event.
    :param page_load_timeout: seconds for load phase.
    :param extra_wait_ms: post-load settle time.
    :param concurrency: worker concurrency.
    :param max_pages: hard cap on processed pages.
    :param collect_results: store every ScrapeResult object.
    :param delay_range: (min,max) jitter before first scrape per worker loop.
    :param tab_close_timeout: seconds to wait closing a tab.
    :param proxy_server: (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    :param proxy_bypass_list: (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    :param origins_with_universal_network_access: (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.
    :return: crawl summary
    :rtype: CrawlResult
    """
    # :param scrape_bytes: capture bytes stream when possible.
    # :param request_paused_handler: custom fetch interception handler.
    # **must be a type**—not an instance—so that it can be initiated later with the correct values attached
    # :type request_paused_handler: type[ScrapeRequestPausedHandler]

    if scrape_result_handler is None:
        if not collect_results:
            logger.warning("no `ScrapeResultHandler` provided and collect_results is False, only errors and links will be captured")
        else:
            logger.info("no `ScrapeResultHandler` provided, using default handler functions")
        scrape_result_handler = ScrapeResultHandler()

    # normalize delay range if provided
    if delay_range is not None:
        a, b = delay_range
        if a > b:
            delay_range = (b, a)
        if a < 0 or b < 0:
            delay_range = None  # disallow negative
    logger.info(
        "crawl started for %s (depth=%s concurrency=%s max_pages=%s delay=%s)",
        url, depth, concurrency, max_pages, delay_range,
    )

    root_url = fix_url(url)
    if depth is not None:
        depth = max(0, depth)
    concurrency = max(1, concurrency)

    time_start = datetime.now(UTC)

    # queue: (url, current_depth) where root starts at 0
    queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
    await queue.put((root_url, 0))

    # visited = fully processed
    # all_links_set = discovered (even if not yet processed)
    visited: set[str] = set()
    all_links: list[str] = [root_url]
    all_links_set: set[str] = {root_url}
    successful_links: list[str] = []
    processed_final_urls: set[str] = set()
    failed_links: list[FailedLink] = []
    timed_out_links: list[FailedLink] = []
    # optional heavy list of every response captured
    responses: list[ScrapeResult] = [] if collect_results else None  # type: ignore

    pages_processed = 0
    lock = asyncio.Lock()  # protects shared collections when needed
    # map worker idx -> current url for runtime debugging
    current_processing: dict[int, str] = {}

    async def should_enqueue(link: str) -> bool:
        # canonicalize and de-duplicate discovered links; depth checks are handled
        # by the caller so this function only verifies URL shape and duplication.
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
    else:
        instance = base

    # collect tab close tasks so we can await them before returning
    closing_tasks: list[asyncio.Task] = []

    async def worker(idx: int):
        nonlocal pages_processed
        while True:
            try:
                # wait for next target
                current_url, current_depth = await queue.get()
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
            scrape_result: ScrapeResult | None = None
            try:
                # first scrape (root) is at depth 0
                is_first_depth = current_depth == 0
                # simple delay / jitter if specified; skip if first scrape
                if delay_range is not None and is_first_depth:
                    wait_time = random.uniform(*delay_range)
                    logger.info("waiting %.2f seconds before scraping %s", wait_time, current_url)
                    await asyncio.sleep(wait_time)
                scrape_result = await scrape(
                    base=instance,
                    url=current_url,
                    # scrape_bytes=scrape_bytes,
                    scrape_result_handler=scrape_result_handler,
                    navigation_timeout=navigation_timeout,
                    wait_for_page_load=wait_for_page_load,
                    page_load_timeout=page_load_timeout,
                    extra_wait_ms=extra_wait_ms,
                    new_tab=True,
                    # request_paused_handler=request_paused_handler,
                    proxy_server=proxy_server,
                    proxy_bypass_list=proxy_bypass_list,
                    origins_with_universal_network_access=origins_with_universal_network_access,
                    current_depth=current_depth,
                )
                pages_processed += 1
                # determine final canonical URL (after redirects)
                final_url = getattr(scrape_result, "url", None) or (scrape_result.tab.url if scrape_result.tab else current_url)
                final_url = fix_url(final_url)

                links: list[str] = []
                if scrape_result.timed_out_navigating:
                    timed_out_links.append(FailedLink(current_url, scrape_result.timed_out_navigating, error_obj))
                else:
                    if final_url in processed_final_urls:
                        logger.debug("skip duplicate final url %s (source %s)", final_url, current_url)
                    else:
                        # handler already executed inside scrape(); collect links + finalize bookkeeping
                        processed_final_urls.add(final_url)
                        async with lock:
                            if final_url not in all_links_set:
                                all_links_set.add(final_url)
                                all_links.append(final_url)
                        successful_links.append(final_url)
                        # adopt links extracted / supplied by handler
                        links = scrape_result.links or []

                if collect_results and scrape_result:
                    async with lock:
                        responses.append(scrape_result)

                # enqueue new links only if we processed this final url just now
                if final_url in processed_final_urls and not scrape_result.timed_out_navigating:
                    # if depth is None there is no limit
                    if (depth is None or current_depth < depth) and links:
                        for link in links:
                            if max_pages is not None and pages_processed >= max_pages:
                                break
                            if await should_enqueue(link):
                                await queue.put((fix_url(link), current_depth + 1))

                # close tab (timeout to avoid hang) unless it's the dedicated context tab
                if scrape_result.tab and not (new_window and scrape_result.tab is instance):
                    async def _close_tab(t_: nodriver.Tab):
                        try:
                            await asyncio.wait_for(t_.close(), timeout=5)
                        except Exception:
                            logger.warning("tab close failed or timed out for %s", getattr(t_, 'url', '<unknown>'))
                    closing_tasks.append(asyncio.create_task(_close_tab(scrape_result.tab)))
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

    # wait for any outstanding close tasks (bounded wait) to avoid hanging loop
    if closing_tasks:
        done, pending = await asyncio.wait(closing_tasks, timeout=6)
        for p in pending:
            p.cancel()
        if pending:
            logger.warning("cancelled %d lingering tab close task(s)", len(pending))

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
    # scrape_bytes = True,
    scrape_result_handler: ScrapeResultHandler | None = None,
    *,
    navigation_timeout = 30,
    wait_for_page_load = True,
    page_load_timeout = 60,
    extra_wait_ms = 0,
    new_tab = False,
    new_window = False,
    # request_paused_handler = ScrapeRequestPausedHandler,
    proxy_server: str = None,
    proxy_bypass_list: list[str] = None,
    origins_with_universal_network_access: list[str] = None,
    current_depth: int = 0,
):
    """single page scrape (html + optional bytes + link extraction).

    handles navigation, timeouts, fetch interception, html capture, link parsing and
    cleanup (tab closure, pending task draining).

    if `scrape_result_handler` is provided, `scrape_result_handler.handle()` will
    be called exactly once and awaited before returning the final `ScrapeResult`.

    - **`proxy_server`** — (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    - **`proxy_bypass_list`** — (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    - **`origins_with_universal_network_access`** — (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.

    :param base: reuse provided tab/browser root or create fresh.
    :param url: target url.
    :param scrape_bytes: capture non-text body bytes.
    :param scrape_result_handler: if specified, `scrape_result_handler.handle()` will be called
    and awaited before returning the final `ScrapeResult`.
    :param navigation_timeout: seconds for initial navigation.
    :param wait_for_page_load: await full load event.
    :param page_load_timeout: seconds for load phase.
    :param extra_wait_ms: post-load wait for dynamic content.
    :param new_tab: request new tab.
    :param new_window: request isolated window/context.
    :param proxy_server: (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    :param proxy_bypass_list: (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    :param origins_with_universal_network_access: (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.
    :param current_depth: mostly used by `crawl()` to pass current depth to handlers.
    :return: html/links/bytes/metadata
    :rtype: ScrapeResult
    """
    # :param request_paused_handler: custom fetch interception handler.
    # **must be a type**—not an instance—so that it can be initiated later with the correct values attached
    # :type request_paused_handler: type[ScrapeRequestPausedHandler]
    
    start = time.monotonic()
    url = fix_url(url)

    result = ScrapeResult(url, current_depth=current_depth)
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
    result.tab = tab
    logger.info("scraping %s", url)

    await tab.send(
        cdp.network.set_extra_http_headers(
            headers=cdp.network.Headers({
                "Referer": f"{parsed_url.scheme}://{parsed_url.netloc}",
            })
        )
    )

    # TODO: figure out why request interception 
    # causes this error with sandboxed iframes:
    # my current theory is that intercepting the main page's URL
    # changes the pages origin or something weird like that.
    #
    # Blocked script execution in 'about:blank' because the document's
    # frame is sandboxed and the 'allow-scripts' permission is not set.

    # # use the stock handler unless a custom one is provided
    # request_paused_handler = (request_paused_handler or ScrapeRequestPausedHandler)(
    #     tab, result, url, scrape_bytes
    # )
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
        result.timed_out = nav_response.timed_out
        result.timed_out_navigating = nav_response.timed_out_navigating
        result.timed_out_loading = nav_response.timed_out_loading

        if not nav_response.timed_out_navigating:
            # if it's taking forever to load, get_content() will also take forever to load
            result.html = await result.tab.evaluate("document.documentElement.outerHTML")
            if isinstance(result.html, Exception):
                raise result.html
            # use the final URL as the base for extracting links
            result.links = extract_links(result.html, result.url)
        
        # run handler + teardown before possibly closing the tab
        if scrape_result_handler:
            # run handler only if links not already populated to avoid double execution under crawl()
            try:
                result.links = await scrape_result_handler.handle(result)
            except Exception:
                logger.exception("error running scrape_result_handler for %s", url)
        result.elapsed = timedelta(seconds=time.monotonic() - start)
        elapsed_seconds = result.elapsed.total_seconds()
        logger.info("successfully finished scrape for %s (elapsed=%.2fs)", url, elapsed_seconds)

        # await request_paused_handler.stop()
    except Exception:
        result.elapsed = timedelta(seconds=time.monotonic() - start)
        elapsed_seconds = result.elapsed.total_seconds()
        logger.exception(
            "unexpected error during scrape for %s (elapsed=%.2fs):", url, elapsed_seconds
        )

    return result


async def click_template_image(
    tab: nodriver.Tab,
    template: str | os.PathLike,
    *,
    x_shift: int = 0,
    y_shift: int = 0,
    flash_point: bool = False,
    save_annotated_screenshot: str | os.PathLike = None,
    match_threshold: float = 0.5,
):
    """find a template in the current page screenshot and somewhere around it
    (`x_shift` and `y_shift`).

    if the template filename follows this pattern:
    """\
    r"- `{name}__x{-?\d+}__y{-?\d+}`,"\
    """

    then the embedded shifts will be applied unless 
    `x_shift` or `y_shift` are explicitly set to non-zero values.

    :param tab: target nodriver Tab.
    :param template: path or filename of the template image.
    :param x_shift: horizontal shift in css pixels to apply to the computed click point.
    :param y_shift: vertical shift in css pixels to apply to the computed click point.
    :param save_annotated_screenshot: if set, save an annotated screenshot with the matched template.
    :param flash_point: if True, flash the click location after clicking.
    :param match_threshold: only issue a click if the template match exceeds this threshold (0.0-1.0).
    """

    # resolve template path
    tpl_path = Path(template)

    # parse embedded shift hints unless explicitly overridden by params
    # pattern: {name}__  (both optional)
    x_group = re.match(r".*__x(-?\d+).*", tpl_path.name)
    y_group = re.match(r".*__y(-?\d+).*", tpl_path.name)
    if x_group:
        if x_shift == 0:
            x_shift = int(x_group.group(1))
    if y_group:
        if y_shift == 0:
            y_shift = int(y_group.group(1))

    # capture screenshot + dpr
    png_bytes = None
    dpr = float(await tab.evaluate("window.devicePixelRatio||1"))
    await tab.send(cdp.page.enable())
    data = await tab.send(cdp.page.capture_screenshot(format_="png", from_surface=True))
    png_bytes = base64.b64decode(data)

    scr = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    template_im = cv2.imread(str(tpl_path))
    match = cv2.matchTemplate(scr, template_im, cv2.TM_CCOEFF_NORMED)
    _min_v, _max_v, _min_l, max_l = cv2.minMaxLoc(match)
    # matchTemplate returns a normalized score in _max_v; convert to percentage for logging
    match_pct = float(_max_v) * 100.0
    xs, ys = max_l
    th, tw = template_im.shape[:2]
    xe, ye = xs + tw, ys + th
    cx_img = (xs + xe) // 2
    cy_img = (ys + ye) // 2
    # apply shifts (shift already in image pixel space; adjust for dpr afterwards)
    cx_shifted = cx_img + int(x_shift * dpr)
    cy_shifted = cy_img + int(y_shift * dpr)
    h, w = scr.shape[:2]
    cx_shifted = max(0, min(cx_shifted, w - 1))
    cy_shifted = max(0, min(cy_shifted, h - 1))
    # optionally annotate and save an annotated screenshot before clicking
    if save_annotated_screenshot:
        # draw a bright red rectangle around the matched template and a bright green dot at click
        # scr is a BGR image
        rect_thickness = max(2, int(round(3 * (dpr or 1.0))))
        dot_radius = max(3, int(round(4 * (dpr or 1.0))))
        # rectangle: top-left (xs,ys), bottom-right (xe,ye)
        cv2.rectangle(scr, (int(xs), int(ys)), (int(xe), int(ye)), (0, 0, 255), thickness=rect_thickness)
        # dot: filled circle at clicked image coords
        cv2.circle(scr, (int(cx_shifted), int(cy_shifted)), dot_radius, (0, 255, 0), thickness=-1)
        out_path = Path(save_annotated_screenshot)
        # ensure parent dir exists
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # write annotated image
        cv2.imwrite(str(out_path), scr)

    if match_pct / 100.0 < match_threshold:
        logger.debug(
            "skipping template %s: match=%.1f%% img=(%d,%d) css=(%.1f,%.1f) dpr=%.2f shift=(%d,%d)",
            tpl_path.name,
            match_pct,
            cx_shifted,
            cy_shifted,
            cx_shifted / (dpr or 1.0),
            cy_shifted / (dpr or 1.0),
            dpr,
            x_shift,
            y_shift,
        )
        return False

    # convert to css coords
    css_x = cx_shifted / (dpr or 1.0)
    css_y = cy_shifted / (dpr or 1.0)
    await tab.mouse_click(css_x, css_y)
    logger.debug(
        "successfully clicked best match coords for %s: match=%.1f%% img=(%d,%d) css=(%.1f,%.1f) dpr=%.2f shift=(%d,%d)",
        tpl_path.name,
        match_pct,
        cx_shifted,
        cy_shifted,
        css_x,
        css_y,
        dpr,
        x_shift,
        y_shift,
    )
    if flash_point:
        await tab.flash_point(int(css_x), int(css_y))
    return True


__all__ = [
    "wait_for_page_load",
    "get",
    "get_with_timeout",
    "get_user_agent",
    "crawl",
    "scrape",
    "click_template_image",
]