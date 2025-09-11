import logging
import asyncio
import nodriver
import time
from datetime import timedelta
from .scrape_result import (
    ScrapeResult, 
)

logger = logging.getLogger("nodriverplus.browser")

async def get(
    base: nodriver.Tab | nodriver.Browser,
    url: str = "about:blank",
    *,
    new_tab: bool = False,
    new_window: bool = True,
    new_context: bool = True,
    dispose_on_detach: bool = True,
    proxy_server: str = None,
    proxy_bypass_list: list[str] = None,
    origins_with_universal_network_access: list[str] = None,
) -> nodriver.Tab:
    """central factory for new/reused tabs/windows/contexts.

    honors combinations of `new_window`/`new_tab`/`new_context` on `base`.
    
    see https://github.com/twarped/nodriver/commit/1dcb52e8063bad359a3f2978b83f44e20dfbca68

    - **`dispose_on_detach`** — (EXPERIMENTAL) (Optional) If specified, disposes this context when debugging session disconnects.
    - **`proxy_server`** — (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    - **`proxy_bypass_list`** — (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    - **`origins_with_universal_network_access`** — (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.

    :param base: existing tab or browser (defaults to browser root).
    :param url: initial navigation (about:blank by default).
    :param new_window: request a separate window (may create context).
    :param new_tab: request a new tab in existing window/context.
    :param new_context: create an isolated context when opening window.
    :param dispose_on_detach: (EXPERIMENTAL) (Optional) If specified, disposes this context when debugging session disconnects.
    :param proxy_server: (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
    :param proxy_bypass_list: (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
    :param origins_with_universal_network_access: (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.
    :return: acquired/created tab
    :rtype: Tab
    """

    # create new browser context/window with a proxy if specified
    if (new_window and new_context or proxy_server):
        if isinstance(base, nodriver.Tab):
            base = base.browser
        if proxy_server:
            new_window = True
        tab = await base.create_context(
            url=url,
            new_tab=new_tab,
            new_window=new_window,
            dispose_on_detach=dispose_on_detach,
            proxy_server=proxy_server,
            proxy_bypass_list=proxy_bypass_list,
            origins_with_universal_network_access=origins_with_universal_network_access,
        )
        return tab
    # new standalone window without context
    if new_window and isinstance(base, nodriver.Browser):
        return await base.get(url, new_tab=False, new_window=True)
    # open new tab off the browser root
    if new_tab and isinstance(base, nodriver.Browser):
        return await base.get(url, new_tab=True)
    # base is a tab:
    return await base.get(url, new_tab=new_tab, new_window=new_window)


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

    returns a partial ScrapeResult (timing + timeout flags + tab ref)

    :param tab: existing tab or browser root.
    :param url: target url.
    :param navigation_timeout: seconds for navigation phase.
    :param wait_for_page_load_: whether to wait for load event.
    :param page_load_timeout: seconds for load phase.
    :param extra_wait_ms: post-load wait for dynamic content.
    :param new_tab: request new tab first.
    :param new_window: request new window/context first.
    :return: partial ScrapeResult (timing + timeout flags).
    :rtype: ScrapeResult
    """
    result = ScrapeResult(url=url, tab=target, timed_out=True)

    start = time.monotonic()
    # prepare target first when we need a new tab/window/context
    base = target
    if new_tab or new_window:
        try:
            base = await get(
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
        result.tab = base
    except asyncio.TimeoutError:
        result.timed_out_navigating = True
        result.elapsed = timedelta(seconds=time.monotonic() - start)
        logger.warning("timed out getting %s (navigation phase) (elapsed=%.2fs)", url, result.elapsed.total_seconds())
        return result

    if wait_for_page_load_:
        # avoid circular import at module import time by importing locally
        from .tab import wait_for_page_load
        load_task = asyncio.create_task(wait_for_page_load(base, extra_wait_ms))
        try:
            # same thing here
            await asyncio.wait_for(
                asyncio.shield(load_task), 
                timeout=page_load_timeout + extra_wait_ms / 1000
            )
        except asyncio.TimeoutError:
            result.timed_out_loading = True
            result.elapsed = timedelta(seconds=time.monotonic() - start)
            logger.warning("timed out getting %s (load phase) (elapsed=%.2fs)", url, result.elapsed.total_seconds())
            # wait for task to actually cancel
            if not load_task.done():
                load_task.cancel()
                try:
                    await load_task
                except asyncio.CancelledError:
                    pass
            return result

    result.timed_out = False
    result.elapsed = timedelta(seconds=time.monotonic() - start)
    return result


async def stop(browser: nodriver.Browser, graceful = True):
    """stop browser process (optionally wait for graceful exit).

    :param graceful: wait for underlying process to exit.
    """
    logger.info("stopping browser")
    browser.stop()
    if graceful and browser._process is not None:
        logger.info("waiting for graceful shutdown")
        await browser._process.wait()
    logger.info("successfully shutdown browser")


__all__ = [
    "get",
    "get_with_timeout",
    "stop",
]