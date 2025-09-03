import logging
import asyncio
import json
import nodriver
import time
from datetime import timedelta
from nodriver import cdp
from urllib.parse import urlparse
from ..utils import fix_url, extract_links
from ..js.load import load_text as load_js
from .scrape_response import ScrapeResponse
from .pause_handlers.requests import RequestPausedHandler
from .user_agent import UserAgent
from . import cloudflare

logger = logging.getLogger(__name__)


async def acquire_tab(
    base: nodriver.Tab | nodriver.Browser | None = None,
    url: str = "about:blank",
    *,
    new_window: bool = False,
    new_tab: bool = False,
    new_context: bool = True,
) -> nodriver.Tab:
    """central factory for new/reused tabs/windows/contexts.

    honors combinations of `new_window`/`new_tab`/`new_context` on `base`.
    
    see https://github.com/twarped/nodriver/commit/1dcb52e8063bad359a3f2978b83f44e20dfbca68

    :param base: existing tab or browser (defaults to browser root).
    :param new_window: request a separate window (may create context).
    :param new_tab: request a new tab in existing window/context.
    :param new_context: create an isolated context when opening window.
    :param initial_url: initial navigation (about:blank by default).
    :return: acquired tab
    :rtype: nodriver.Tab
    """

    # context+window: gives us isolated storage and a dedicated window
    if new_window and new_context:
        tab = await base.create_context(url, new_window=True)
        return tab
    # new standalone window without context
    if new_window and isinstance(base, nodriver.Browser):
        return await base.get(url, new_tab=False, new_window=True)
    # open new tab off the browser root
    if new_tab and isinstance(base, nodriver.Browser):
        return await base.get(url, new_tab=True)
    # base is a tab:
    return await base.get(url, new_tab=new_tab, new_window=new_window)


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
    logger.info(
        "user agent data retrieved from %s:\nua=%s\nplatform=%s\nacceptLanguage=%s\nmetadata=%s",
        tab.url,
        user_agent.user_agent,
        user_agent.platform,
        user_agent.accept_language,
        bool(user_agent.metadata),
    )
    return user_agent


async def scrape( 
    url: str,
    target: nodriver.Tab | nodriver.Browser,
    *,
    scrape_bytes = True,
    navigation_timeout = 30,
    wait_for_page_load = True,
    page_load_timeout = 60,
    extra_wait_ms = 0,
    # solve_cloudflare = True, # not implemented yet
    new_tab = False,
    new_window = False,
    wait_for_pending_fetches: bool = True,
    request_handler: RequestPausedHandler | None = None,
):
    """single page scrape (html + optional bytes + link extraction).

    handles navigation, timeouts, fetch interception, html capture, link parsing and
    cleanup (tab closure, pending task draining).

    :param url: target url.
    :param scrape_bytes: capture non-text body bytes.
    :param existing_tab: reuse provided tab/browser root or create fresh.
    :param navigation_timeout: seconds for initial navigation.
    :param wait_for_page_load: await full load event.
    :param page_load_timeout: seconds for load phase.
    :param extra_wait_ms: post-load wait for dynamic content.
    :param new_tab: request new tab.
    :param new_window: request isolated window/context.
    :param wait_for_pending_fetches: await in-flight fetch interception tasks.
    :param request_handler: optional custom RequestPausedHandler (overrides scrape_bytes logic).
    :return: html/links/bytes/metadata
    :rtype: ScrapeResponse
    """
    start = time.monotonic()
    pending_tasks: set[asyncio.Task] = set()
    url = fix_url(url)

    scrape_response = ScrapeResponse(url)
    parsed_url = urlparse(url)

    # central acquisition
    tab = await acquire_tab(
        target, 
        new_window=new_window, 
        new_tab=new_tab, 
    )
    scrape_response.tab = tab
    logger.info("scraping %s", url)

    # network prep:
    # cache is disabled so we can actually get bytes
    await tab.send(cdp.network.enable())
    await tab.send(cdp.network.set_cache_disabled(True))
    await tab.send(
        cdp.network.set_extra_http_headers(
            headers=cdp.network.Headers({
                "Referer": f"{parsed_url.scheme}://{parsed_url.netloc}",
            })
        )
    )

    # we want the headers for the ScrapeResponse
    async def on_response(ev: cdp.network.ResponseReceived):
        if ev.response.url != url:
            return
        scrape_response.headers = {k.lower(): v for k, v in (ev.response.headers or {}).items()}
        scrape_response.mime = ev.response.mime_type.lower().split(";", 1)[0].strip()
        # one-shot: we just need the main response
        tab.remove_handler(cdp.network.ResponseReceived, on_response)
    tab.add_handler(cdp.network.ResponseReceived, on_response)

    # interception pipeline (always instantiate / attach)
    # if caller provides custom handler we honor its config (may ignore scrape_bytes)
    # otherwise create handler with capture_main_body tied to scrape_bytes flag
    fetch_handler = None
    if request_handler is not None:
        rp_handler = request_handler
    else:
        # lazy import config class only if building
        from .pause_handlers.requests import RequestInterceptionConfig
        rp_handler = RequestPausedHandler(
            config=RequestInterceptionConfig(capture_main_body=bool(scrape_bytes))
        )

    def on_fetch(ev: cdp.fetch.RequestPaused):
        task = asyncio.create_task(rp_handler.handle(tab, ev))
        pending_tasks.add(task)
        task.add_done_callback(lambda t: pending_tasks.discard(t))
        return None

    tab.add_handler(cdp.fetch.RequestPaused, on_fetch)
    fetch_handler = on_fetch

    error_obj: Exception = None
    try:
        nav_response = await get_with_timeout(tab, url, 
            navigation_timeout=navigation_timeout, 
            wait_for_page_load_=wait_for_page_load, 
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
                # capture for caller logging; keep html empty string so downstream link extraction is safe
                error_obj = nodriver.ProtocolException(val)
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
    except Exception as e:
        error_obj = e
    finally:
        # ensure we remove any fetch handler we installed
        if fetch_handler is not None:
            try:
                tab.remove_handler(cdp.fetch.RequestPaused, fetch_handler)
            except Exception:
                logger.debug("failed removing fetch handler during scrape cleanup for %s", url)
            # wait for any in-flight fetch tasks to finish (bounded)
            if wait_for_pending_fetches:
                try:
                    tasks_to_wait = set(pending_tasks)
                    if tasks_to_wait:
                        logger.debug("waiting for %d pending fetch tasks for %s", len(tasks_to_wait), url)
                        done, pending = await asyncio.wait(tasks_to_wait, timeout=2)
                        if pending:
                            logger.debug("cancelling %d pending fetch tasks for %s", len(pending), url)
                            for t in pending:
                                try:
                                    t.cancel()
                                except Exception:
                                    pass
                            try:
                                await asyncio.wait(pending, timeout=1)
                            except Exception:
                                pass
                except Exception:
                    logger.debug("error while waiting or cancelling pending fetch tasks for %s", url)
            else:
                for t in list(pending_tasks):
                    if not t.done():
                        t.cancel()

        scrape_response.bytes_ = rp_handler.main_body_bytes
        scrape_response.redirect_chain = rp_handler.redirect_chain

    scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
    elapsed_seconds = scrape_response.elapsed.total_seconds()
    if error_obj is not None:
        logger.exception("unexpected error during scrape for %s (elapsed=%.2fs): %s", url, elapsed_seconds, error_obj)
    else:
        logger.info("successfully finished scrape for %s (elapsed=%.2fs)", url, elapsed_seconds)

    return scrape_response
    

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


async def get_with_timeout( 
    target: nodriver.Tab | nodriver.Browser, 
    url: str, 
    *,
    navigation_timeout = 30,
    wait_for_page_load_ = True,
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
    scrape_response = ScrapeResponse(url, target, True)

    start = time.monotonic()
    # prepare target first when we need a new tab/window/context
    base = target
    if new_tab or new_window:
        try:
            base = await acquire_tab(
                target,
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

    if wait_for_page_load_:    
        load_task = asyncio.create_task(wait_for_page_load(base, extra_wait_ms))
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
