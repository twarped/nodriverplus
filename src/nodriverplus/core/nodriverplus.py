"""scrape/crawl helpers layered over `nodriver`.

wraps nodriver to add:
- user agent acquisition + patch (network / emulation / runtime) with headless token scrub
- single page scrape (html + optional raw bytes + link extraction)
- crawl (depth, concurrency, jitter, per-page handler, error + timeout tracking)
- fetch interception to stream non-text main bodies (e.g. pdf) before chrome consumes them
- lightweight cloudflare challenge surface detection (caller can extend)

keeps low-level access to the underlying nodriver Browser so callers can still drive CDP directly.
definitely still needs work tho
"""
import logging
import os
import nodriver
from types import CoroutineType
from nodriver import Config
from .user_agent import *
from .scrape_result import *
from .tab import get_user_agent, scrape, crawl
from .browser import get, get_with_timeout, stop
from .manager import Manager
from .handlers import TargetInterceptor, TargetInterceptorManager, NetworkWatcher
from .handlers.stock import (
    UserAgentPatch, 
    # not working as intended currently
    # ScrapeRequestPausedHandler,
    WindowSizePatch,
    CloudflareSolver,
)

logger = logging.getLogger("nodriverplus.NodriverPlus")


class NodriverPlus:
    """high-level orchestrator for starting a stealthy browser and performing scrapes/crawls.

    lifecycle:
    1. `start()`: launch chrome and fetch + patch user agent
    2. `scrape()`: navigate + capture html / links / headers / optional bytes
    3. `crawl()`: customizable nodriver crawling API with depth + concurrency + handler-produced link expansion
    4. `stop()`: shutdown underlying process (optional graceful wait)

    ua patching: applies Network + Emulation overrides + runtime JS patch to sync navigator.* / userAgentData.
    """
    browser: nodriver.Browser
    config: nodriver.Config
    user_agent: UserAgent
    interceptor_manager: TargetInterceptorManager

    def __init__(self, 
        user_agent: UserAgent = None, 
        hide_headless: bool = True, 
        solve_cloudflare: bool = True,
        *,
        save_annotated_screenshot: str | os.PathLike = None,
        interceptors: list[TargetInterceptor] = None,
        network_watchers: list[NetworkWatcher] = None,
        manager_concurrency: int = 1,
    ):
        """initialize a `NodriverPlus` instance
        
        :param user_agent: `UserAgent` to patch browser with 
        using stock interceptor: `UserAgentInterceptor`

        :param interceptors: list of additional custom interceptors to apply
        """
        self.config = Config()
        self.browser = None
        self.user_agent = user_agent
        self.hide_headless = hide_headless
        # init interceptor manager and add provided + stock interceptors
        interceptor_manager = TargetInterceptorManager(interceptors, network_watchers)
        if user_agent:
            interceptor_manager.interceptors.append(UserAgentPatch(user_agent, hide_headless))
        if solve_cloudflare:
            interceptor_manager.network_watchers.append(CloudflareSolver(save_annotated_screenshot))
        self.interceptor_manager = interceptor_manager
        # dedicated queue manager (jobs provide their own per-job crawl/scrape concurrency)
        self.manager = Manager(concurrency=manager_concurrency)


    # TODO: make a `WindowSize` dataclass that can be passed
    # so that users can specify other meta like
    # `device_scale_factor`, `mobile`, and `orientation`
    async def start(self,
        config: Config | None = None,
        *,
        window_size: tuple[int, int] | None = (1920, 1080),
        user_data_dir: os.PathLike | None = None,
        headless: bool | None = False,
        browser_executable_path: os.PathLike | None = None,
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

        wraps nodriver.start then optionally fetches + patches the user agent. returns the raw
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
        browser_args = browser_args or []
        if window_size:
            width, height = window_size
            # remove any existing --window-size arg to avoid dupes
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
                UserAgentPatch(self.user_agent, self.hide_headless)
            )

        await UserAgentPatch.patch_user_agent(
            self.browser.main_tab, 
            None,
            self.user_agent,
            self.hide_headless
        )

        if window_size:
            width, height = window_size
            await WindowSizePatch.patch_window_size(
                self.browser.main_tab,
                None,
                width=width,
                height=height,
            )

        # start the `TargetInterceptorManager`
        self.interceptor_manager.connection = self.browser.connection
        await self.interceptor_manager.start()

        self.config = self.browser.config

        return self.browser


    async def crawl(self,
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
    ):
        """customizable crawl API starting at `url` up to `depth`.

        schedules scrape tasks with a worker pool, collects response metadata, errors,
        links, and timing. handler is invoked for each page producing optional new links.

        if `crawl_result_handler` is specified, `crawl_result_handler.handle()` will 
        be called and awaited before returning the final `CrawlResult`.

        `crawl_result_handler` is nifty if you're crawling with a `Manager`
        instance.

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
        :return: crawl summary
        :rtype: CrawlResult
        """
        # :param scrape_bytes: capture bytes stream when possible.
        # :param request_paused_handler: custom fetch interception handler.
        # **must be a type**—not an instance—so that it can be initiated later with the correct values attached
        # :type request_paused_handler: type[ScrapeRequestPausedHandler]

        return await crawl(
            self.browser,
            url=url,
            scrape_result_handler=scrape_result_handler,
            depth=depth,
            crawl_result_handler=crawl_result_handler,
            new_window=new_window,
            # scrape_bytes=scrape_bytes,
            navigation_timeout=navigation_timeout,
            wait_for_page_load=wait_for_page_load,
            page_load_timeout=page_load_timeout,
            extra_wait_ms=extra_wait_ms,
            concurrency=concurrency,
            max_pages=max_pages,
            collect_results=collect_results,
            delay_range=delay_range,
            # request_paused_handler=request_paused_handler,
        )


    async def scrape(self, 
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
    ):
        """single page scrape (html + optional bytes + link extraction).

        handles navigation, timeouts, fetch interception, html capture, link parsing and
        cleanup (tab closure, pending task draining).

        if `scrape_result_handler` is provided, `scrape_result_handler.handle()` will
        be called and awaited before returning the final `ScrapeResult`.

        - **`proxy_server`** — (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
        - **`proxy_bypass_list`** — (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
        - **`origins_with_universal_network_access`** — (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.

        ### not implemented yet:
        - `scrape_result_handler` could be useful if you want to execute stuff on `tab`
        after the page loads, but before the `RequestPausedHandler` is removed

        :param url: target url.
        :param scrape_result_handler: if specified, `scrape_result_handler.handle()` will be called 
        :param existing_tab: reuse provided tab/browser root or create fresh.
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
        :return: html/links/bytes/metadata
        :rtype: ScrapeResult
        """
        # :param scrape_bytes: capture non-text body bytes.
        # :param request_paused_handler: custom fetch interception handler.
        # **must be a type**—not an instance—so that it can be initiated later with the correct values attached
        # :type request_paused_handler: type[ScrapeRequestPausedHandler]
        
        return await scrape(
            base=self.browser,
            url=url,
            # scrape_bytes=scrape_bytes,
            scrape_result_handler=scrape_result_handler,
            navigation_timeout=navigation_timeout,
            wait_for_page_load=wait_for_page_load,
            page_load_timeout=page_load_timeout,
            extra_wait_ms=extra_wait_ms,
            new_tab=new_tab,
            new_window=new_window,
            # request_paused_handler=request_paused_handler,
            proxy_server=proxy_server,
            proxy_bypass_list=proxy_bypass_list,
            origins_with_universal_network_access=origins_with_universal_network_access
        )


    async def get_with_timeout(self, 
        url: str, 
        *,
        navigation_timeout = 30,
        wait_for_page_load = True,
        page_load_timeout = 60,
        extra_wait_ms = 0,
        new_tab = False,
        new_window = False
    ) -> CoroutineType[any, any, ScrapeResult]:
        """navigate with separate navigation + load timeouts.

        returns a partial ScrapeResult (timing + timeout flags + tab ref)

        :param tab: existing tab or browser root.
        :param url: target url.
        :param navigation_timeout: seconds for navigation phase.
        :param wait_for_page_load: whether to wait for load event.
        :param page_load_timeout: seconds for load phase.
        :param extra_wait_ms: post-load wait for dynamic content.
        :param new_tab: request new tab first.
        :param new_window: request new window/context first.
        :return: partial ScrapeResult (timing + timeout flags).
        :rtype: ScrapeResult
        """
        return get_with_timeout(
            target=self.browser,
            url=url,
            navigation_timeout=navigation_timeout,
            wait_for_page_load=wait_for_page_load,
            page_load_timeout=page_load_timeout,
            extra_wait_ms=extra_wait_ms,
            new_tab=new_tab,
            new_window=new_window
        )
    

    async def get(self,
        url: str = "about:blank",
        *,
        new_tab: bool = False,
        new_window: bool = True,
        new_context: bool = True,
        dispose_on_detach: bool = True,
        proxy_server: str = None,
        proxy_bypass_list: list[str] = None,
        origins_with_universal_network_access: list[str] = None,
    ):
        """central factory for new/reused tabs/windows/contexts.

        honors combinations of `new_window`/`new_tab`/`new_context` on `base`.
        
        see https://github.com/twarped/nodriver/commit/1dcb52e8063bad359a3f2978b83f44e20dfbca68

        - **`dispose_on_detach`** — (EXPERIMENTAL) (Optional) If specified, disposes this context when debugging session disconnects.
        - **`proxy_server`** — (EXPERIMENTAL) (Optional) Proxy server, similar to the one passed to --proxy-server
        - **`proxy_bypass_list`** — (EXPERIMENTAL) (Optional) Proxy bypass list, similar to the one passed to --proxy-bypass-list
        - **`origins_with_universal_network_access`** — (EXPERIMENTAL) (Optional) An optional list of origins to grant unlimited cross-origin access to. Parts of the URL other than those constituting origin are ignored.

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
        return get(
            base=self.browser,
            url=url,
            new_tab=new_tab,
            new_window=new_window,
            new_context=new_context,
            dispose_on_detach=dispose_on_detach,
            proxy_server=proxy_server,
            proxy_bypass_list=proxy_bypass_list,
            origins_with_universal_network_access=origins_with_universal_network_access
        )


    async def stop(self, graceful = True):
        """stop browser and underlying `Manager` process (optionally wait for graceful exit).

        :param graceful: wait for underlying process to exit.
        """
        await self.manager.stop()
        await self.interceptor_manager.stop()
        await stop(self.browser, graceful)


    def enqueue_crawl(self, *args, **kwargs):
        """enqueue a crawl job using the internal `Manager`.

        auto-starts manager loop on first use if not already running.
        first positional argument must be the seed url.
        """
        if not self.browser:
            raise RuntimeError("browser not started")
        # manager expects (base, url, ...)
        if self.manager._runner_task is None:
            self.manager.start()
        self.manager.enqueue_crawl(self.browser, *args, **kwargs)


    def enqueue_scrape(self, *args, **kwargs):
        """enqueue a scrape job using the internal `Manager`.

        auto-starts manager loop on first use if not already running.
        first positional argument must be the target url.
        """
        if not self.browser:
            raise RuntimeError("browser not started")
        if self.manager._runner_task is None:
            self.manager.start()
        self.manager.enqueue_scrape(self.browser, *args, **kwargs)


    async def wait_for_queue(self, timeout: float | None = None):
        """await completion of all queued manager jobs."""
        await self.manager.wait_for_queue(timeout)


    async def start_manager(self, concurrency: int | None = None):
        """start the internal `Manager` loop if not already running."""
        if self.manager._runner_task is None:
            self.manager.concurrency = concurrency or self.manager.concurrency
            self.manager.start()


    async def stop_manager(self, timeout: float | None = None):
        """gracefully stop internal manager and export remaining jobs."""
        return await self.manager.stop(timeout)