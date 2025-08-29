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
import json
import logging
from os import PathLike
import asyncio, base64, re
from urllib.parse import urlparse, urljoin
import urlcanon
import nodriver
from nodriver import Config, cdp
from .cdp_helpers import TARGET_DOMAINS, can_use_domain
from .user_agent import *
from .scrape_response import *
from ..utils import extract_links
from ..js.load import load_text as load_js
from . import cloudflare
from datetime import timedelta

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

    def __init__(self, user_agent: UserAgent = None, stealth: bool = True):
        self.config = Config()
        self.browser = None
        self.user_agent = user_agent
        self.stealth = stealth

    async def start(self,
        config: Config | None = None,
        *,
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
        """launch a browser and prime stealth / ua state.

        wraps nodriver.start then optionally fetches + patches the user agent and installs
        stealth scripts (shadow root auto-attach, navigator tweaks, etc.). returns the raw
        nodriver Browser so callers can still use low-level APIs.

        :param config: optional pre-built Config.
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

        if not self.user_agent:
            self.user_agent = await self.get_user_agent(self.browser.main_tab)
        await self.patch_user_agent(self.browser.main_tab, self.user_agent)

        if self.stealth:
            await self.setup_stealth(self.browser)

        self.config = self.browser.config
        return self.browser


    async def send_message(
        self,
        method: str,
        params: dict = {},
        session_id: str = None,
        connection: nodriver.Tab | nodriver.Connection = None
    ):
        """thin helper/patch to send a raw devtools command.

        yields a dict suitable for nodriver's generator-based send() made specifically for this `nodriver` patch: 
        
        https://github.com/twarped/nodriver/commit/bf1dfda6cb16a31d2fd302f370f130dda3a3413b

        :param method: full method name (e.g. "Runtime.evaluate").
        :param params: payload dict; omit if none.
        :param session_id: target session if addressing a child target.
        :param connection: override connection (tab or underlying connection).
        :return: protocol response pair (varies by method) or None.
        """
        def c():
            yield {"method": method, "params": params, "sessionId": session_id}

        if connection:
            return await connection.send(c())
        return await self.browser.connection.send(c())


    async def autohook_stealth(self, connection: nodriver.Tab | nodriver.Connection, ev: cdp.target.AttachedToTarget = None):
        """enable Target.setAutoAttach with our filter + resume logic.

        attaches recursively to workers/frames and ensures stealth patches + user agent
        overrides are applied before scripts run. idempotent per connection.

        :param connection: tab/connection/browsers connection.
        :param ev: optional original attach event (when recursively called).
        """
        types = list(TARGET_DOMAINS.keys())
        types.remove("tab")

        # hangs on service workers if not deduped
        if getattr(connection, "_stealth_auto_attached", False):
            return
        setattr(connection, "_stealth_auto_attached", True)

        try:
            await self.send_message("Target.setAutoAttach", {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
                "filter": [{"type": t, "exclude": False} for t in types]
            }, ev.session_id if ev else None)
        except Exception:
            logger.exception("auto attach failed for %s:", 
                f"{ev.target_info.type_} <{ev.target_info.url}>" if ev else connection)


    async def on_attach_stealth(self, ev: cdp.target.AttachedToTarget, session: nodriver.Tab | nodriver.Browser | nodriver.Connection):        
        """handler fired when a new target is auto-attached.

        applies stealth js + ua patch, then resumes the debugger

        :param ev: CDP AttachedToTarget event.
        :param session: object providing .connection (browser or tab or connection itself).
        """
        connection = session.connection if isinstance(session, nodriver.Browser) else session

        logger.debug("successfully attached to %s", f"{ev.target_info.type_} <{ev.target_info.url}>")
        # apply patches
        await self.apply_stealth(ev)
        await self.patch_user_agent(ev, self.user_agent)
        # recursive attachment
        await self.autohook_stealth(connection, ev)
        # continue like normal
        msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
        try:
            await self.send_message("Runtime.runIfWaitingForDebugger", session_id=ev.session_id)
        except Exception as e:
            if "-3200" in str(e):
                logger.warning("too slow resuming %s", msg)
            else:
                logger.exception("failed to resume %s:", msg)
        else:
            logger.debug("successfully resumed %s", msg)


    async def apply_stealth(self, ev: cdp.target.AttachedToTarget):
        """inject stealth patch into a target (runtime + early document script).

        chooses worker/page variant based on target type.

        :param ev: attach event describing the target.
        """
        # load and apply the stealth patch
        name = "apply_stealth.js"
        if ev.target_info.type_ in {"service_worker", "shared_worker"}:
            name = "apply_stealth_worker.js"
        js = load_js(name)
        msg = f"{ev.target_info.type_} <{ev.target_info.url}>"

        # try adding the patch to the page
        try:
            if can_use_domain(ev.target_info.type_, "Page"):
                await self.send_message("Page.enable", session_id=ev.session_id)
                await self.send_message("Page.addScriptToEvaluateOnNewDocument", {
                    "source": js,
                    "includeCommandLineAPI": True,
                    "runImmediately": True
                }, ev.session_id)
                logger.debug("successfully added script to %s", msg)
        except Exception:
            logger.exception("failed to add script to %s:", msg)

        try:
            await self.send_message("Runtime.evaluate", {
                "expression": js,
                "includeCommandLineAPI": True,
                "awaitPromise": True
            }, session_id=ev.session_id)
        except Exception as e:
            if "-3200" in str(e):
                logger.warning("too slow patching %s", msg)
            else:
                logger.exception("failed to patch %s:", msg)
        else:
            logger.debug("successfully applied patch to %s", msg)


    async def setup_stealth(self, session: nodriver.Tab | nodriver.Browser | nodriver.Connection):
        """one-time setup on a root connection to enable recursive stealth patching.

        :param session: target session for the operation.
        """
        connection = session.connection if isinstance(session, nodriver.Browser) else session

        # avoid duping stuff
        if getattr(connection, "_stealth_initialized", False):
            return
        setattr(connection, "_stealth_initialized", True)

        connection.add_handler(cdp.target.AttachedToTarget, self.on_attach_stealth)
        await self.autohook_stealth(connection)


    async def get_user_agent(self, tab: nodriver.Tab):
        """evaluate helper script in a tab to extract structured user agent data.

        converts returned json into UserAgent / UserAgentMetadata models.

        :param tab: target tab for the operation.
        :return: structured user agent data.
        :rtype: UserAgent
        """
        js = load_js("get_user_agent.js")
        ua_data: dict = json.loads(await tab.evaluate(js, await_promise=True))
        ua_data["metadata"] = UserAgentMetadata(**ua_data["metadata"]) if ua_data.get("metadata") else None
        user_agent = UserAgent(**ua_data)
        logger.info("successfully retrieved user agent from %s", tab.url)
        logger.debug("user agent data retrieved from %s: \n%s", tab.url, user_agent.to_json())
        return user_agent


    async def patch_user_agent(self, 
        target: cdp.target.AttachedToTarget | nodriver.Tab,
        user_agent: UserAgent,
    ):
        """apply UA overrides across relevant domains for a target.

        removes "Headless" when `self.stealth=True`

        sets Network + Emulation overrides and installs a runtime 
        patch so navigator + related surfaces align. worker/page aware.

        :param target: tab or AttachedToTarget event.
        :param user_agent: prepared UserAgent instance.
        """
        if self.stealth:
            user_agent.user_agent = user_agent.user_agent.replace("Headless", "")
            user_agent.app_version = user_agent.app_version.replace("Headless", "")

        # hacky, but it works
        if isinstance(target, nodriver.Tab):
            is_connection = True
            target_type = "page"
            msg = f"tab <{target.url}>"
            target.session_id = None
        else:
            is_connection = False
            target_type = target.target_info.type_
            msg = f"{target_type} <{target.target_info.url}>"
        domains_patched = []

        if can_use_domain(target_type, "Network"):
            await self.send_message(
                "Network.setUserAgentOverride", 
                user_agent.to_json(), 
                target.session_id, 
                target if is_connection else None
            )
            domains_patched.append("Network")
        if can_use_domain(target_type, "Emulation"):
            await self.send_message(
                "Emulation.setUserAgentOverride", 
                user_agent.to_json(), 
                target.session_id, 
                target if is_connection else None
            )
            domains_patched.append("Emulation")
        if can_use_domain(target_type, "Runtime"):
            js = load_js("patch_user_agent.js")
            uaPatch = f"const uaPatch = {user_agent.to_json(True, True)};"
            await self.send_message("Runtime.evaluate", {
                "expression": js.replace("//uaPatch//", uaPatch),
                "includeCommandLineAPI": True,
            }, target.session_id, target if is_connection else None)
            domains_patched.append("Runtime")

        if len(domains_patched) == 0:
            logger.debug("no domains available to patch user agent for %s", msg)
        else:
            logger.debug("successfully patched user agent for %s with domains %s", msg, domains_patched)


    async def acquire_tab(self,
        *,
        base: nodriver.Tab | nodriver.Browser | None = None,
        new_window: bool = False,
        new_tab: bool = False,
        new_context: bool = True,
        initial_url: str = "about:blank",
    ) -> nodriver.Tab:
        """central factory for new/reused tabs/windows/contexts.

        honors combinations of `new_window`/`new_tab`/`new_context` and falls back to `self.browser`.

        :param base: existing tab or browser (defaults to browser root).
        :param new_window: request a separate window (may create context).
        :param new_tab: request a new tab in existing window/context.
        :param new_context: create an isolated context when opening window.
        :param initial_url: initial navigation (about:blank by default).
        :return: acquired tab
        :rtype: nodriver.Tab
        """
        # central place to create/reuse tabs/windows/contexts
        if base is None:
            base = self.browser
        # context+window: gives us isolated storage and a dedicated window
        if new_window and new_context:
            try:
                tab = await self.browser.create_context(initial_url, new_window=True)
                return tab
            except Exception:
                logger.exception("failed creating context window; falling back to plain window")
        # new standalone window without context
        if new_window and isinstance(base, nodriver.Browser):
            return await base.get(initial_url, new_tab=False, new_window=True)
        # open new tab off a browser root
        if new_tab and isinstance(base, nodriver.Browser):
            return await base.get(initial_url, new_tab=True)
        # base is a tab: delegate; when asking for a new tab it will bubble to browser
        if isinstance(base, nodriver.Tab):
            return await base.get(initial_url, new_tab=new_tab, new_window=new_window)
        # fallback: main tab
        return self.browser.main_tab


    async def crawl(self,
        url: str,
        handler: ScrapeResponseHandler = None,
        depth = 1,
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
        tab_close_timeout: float = 5.0,
        wait_for_pending_fetch: bool = True,
    ):
        """customizable crawl API starting at `url` up to `depth`.

        schedules scrape tasks with a worker pool, collects response metadata, errors,
        links, and timing. handler is invoked for each page producing optional new links.

        :param url: root starting point.
        :param handler: optional ScrapeResponseHandler (auto-created if None).
        :param depth: max link depth (0 means single page).
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
        :param wait_for_pending_fetch: await outstanding fetch interception tasks.
        :return: crawl summary
        :rtype: CrawlResult
        """
        if handler is None:
            if not collect_responses:
                logger.warning("no handler provided and collect_responses is False, only errors and links will be captured")
            else:
                logger.info("no handler provided, using default handler functions")
            handler = ScrapeResponseHandler()

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

        root_url = self.fix_url(url)
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
                link_canon = self.fix_url(link)
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
            instance = await self.acquire_tab(new_window=True, new_context=True)

        async def worker(idx: int):  # noqa: ARG001
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
                        navigation_timeout=navigation_timeout,
                        wait_for_page_load=wait_for_page_load,
                        page_load_timeout=page_load_timeout,
                        extra_wait_ms=extra_wait_ms,
                        new_tab=True,
                        wait_for_pending_fetches=wait_for_pending_fetch,
                    )
                    pages_processed += 1
                    links: list[str] = []
                    try:
                        # remember: the handler can mutate links
                        links = await handler.handle(scrape_response) or []
                    except Exception as e:
                        error_obj = e
                        logger.exception("failed running handler for %s:", current_url)
                    finally:
                        async def _safe_close():
                            # run close in its own task so a hung protocol txn can't block join()
                            if not scrape_response.tab:
                                return
                            # avoid closing the dedicated context's primary tab twice
                            if new_window and scrape_response.tab is instance:
                                return
                            t = asyncio.create_task(scrape_response.tab.close())
                            try:
                                await asyncio.wait_for(t, timeout=tab_close_timeout)
                            except asyncio.TimeoutError:
                                # target likely crashed/detached before answering Target.closeTarget
                                logger.warning("timeout closing tab for %s after %.1fs (continuing)", current_url, tab_close_timeout)
                                t.cancel()
                            except Exception:
                                logger.exception("failed closing tab for %s:", current_url)
                        try:
                            await _safe_close()
                        except Exception:
                            # never let close issues block queue progress
                            logger.exception("unexpected error in safe close for %s", current_url)

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
                            final_url = self.fix_url(final_url)
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
                                await queue.put((self.fix_url(link), next_remaining))
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
        result = CrawlResult(
            links=all_links,
            successful_links=successful_links,
            failed_links=failed_links,
            timed_out_links=timed_out_links,
            time_start=time_start,
            time_end=time_end,
            time_elapsed=time_end - time_start,
            responses=responses,
        )
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
        *,
        navigation_timeout = 30,
        wait_for_page_load = True,
        page_load_timeout = 60,
        extra_wait_ms = 0,
        # solve_cloudflare = True, # not implemented yet
        new_tab = False,
        new_window = False,
        wait_for_pending_fetches: bool = True,
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
        :return: html/links/bytes/metadata
        :rtype: ScrapeResponse
        """
        start = time.monotonic()
        pending_tasks: set[asyncio.Task] = set()
        target = existing_tab or self.browser
        url = self.fix_url(url)

        scrape_response = ScrapeResponse(url)
        parsed_url = urlparse(url)

        if target is None:
            raise ValueError("browser was never started! start with `NodriverPlus.start(**kwargs)`")
        # central acquisition
        tab = await self.acquire_tab(
            base=target, 
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

        # catch those bytes and keep a reference to the handler so we can remove it
        fetch_handler = None
        if scrape_bytes:
            # per-scrape dedupe state so concurrent scrapes don't collide
            active_fetch_interceptions: set[str] = set()
            active_fetch_lock: asyncio.Lock = asyncio.Lock()
            fetch_handler = await self.scrape_bytes(
                url, tab, scrape_response, pending_tasks,
                active_fetch_interceptions=active_fetch_interceptions,
                active_fetch_lock=active_fetch_lock,
            )

        error_obj: Exception = None
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
            scrape_response.url = self.fix_url(final_url)

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
                        # copy the set because tasks remove themselves when done
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
                                # give cancelled tasks a moment to finish
                                try:
                                    await asyncio.wait(pending, timeout=1)
                                except Exception:
                                    pass
                    except Exception:
                        logger.debug("error while waiting for cancelling pending fetch tasks for %s", url)
                else:
                    # best-effort cancel outstanding tasks quickly
                    for t in list(pending_tasks):
                        if not t.done():
                            t.cancel()
                    # no wait; we are intentionally dropping them

        scrape_response.elapsed = timedelta(seconds=time.monotonic() - start)
        elapsed_seconds = scrape_response.elapsed.total_seconds()
        if error_obj is not None:
            logger.exception("unexpected error during scrape for %s (elapsed=%.2fs): %s", url, elapsed_seconds, error_obj)
        else:
            logger.info("successfully finished scrape for %s (elapsed=%.2fs)", url, elapsed_seconds)

        return scrape_response
    

    async def scrape_bytes(self, 
        url: str, 
        tab: nodriver.Tab, 
        scrape_response: ScrapeResponse,
        pending_tasks: set[asyncio.Task],
        *,
        active_fetch_interceptions: set[str],
        active_fetch_lock: asyncio.Lock,
    ):
        """install fetch interception handlers to capture raw response bytes.

        streams non-text main navigation bodies (e.g. pdfs) into scrape_response.bytes_.

        :param url: navigation url (for main nav tracking).
        :param tab: active tab.
        :param scrape_response: response accumulator to populate.
        :param pending_tasks: set collecting async tasks for cleanup.
        :param active_fetch_interceptions: dedupe set for in-flight interceptions.
        :param active_fetch_lock: lock protecting interception state.
        :return: handler function to unregister later.
        """
        await tab.send(cdp.fetch.enable())
        chunks, total_len = [], None
        # track interception lifecycle so we never double-continue
        intercept_states: dict[str, dict] = {}
        # track main navigation across redirects so we only stream final doc once
        main_nav_initial_url = url
        main_nav_current_url = url
        main_nav_request_id: str | None = None
        redirect_chain: list[str] = []
        main_nav_done = False
        # the original handler logic is placed in an inner coroutine so the
        # public handler can create a Task and register it in
        # `self._pending_fetch_tasks`. this ensures tasks are awaited at the
        # end of the scrape and not left pending when the tab/connection
        # closes.
        async def _on_fetch(ev: cdp.fetch.RequestPaused):
            nonlocal total_len, main_nav_initial_url, main_nav_current_url, main_nav_request_id, main_nav_done

            # ignore late events after we've finalized main navigation
            if main_nav_done:
                return

            if ev.response_status_code is None:
                request_headers = {k.lower(): v for k, v in (ev.request.headers or {}).items()}
                logger.debug("successfully intercepted %s request for %s", ev.request.method,ev.request.url)
                scrape_response.intercepted_requests[ev.request.url] = ScrapeRequestIntercepted(
                    url=ev.request.url,
                    headers=request_headers,
                    method=ev.request.method,
                )

                # remove range header
                request_headers.pop("range", None)

                # dedupe concurrent handling of the same interception id using per-scrape lock/set
                req_id = ev.request_id
                async with active_fetch_lock:
                    if req_id in active_fetch_interceptions:
                        logger.debug("skipping duplicate continue_request for %s (id=%s)", ev.request.url, req_id)
                        return
                    active_fetch_interceptions.add(req_id)

                try:
                    if req_id in intercept_states:
                        logger.debug("duplicate request phase for %s (id=%s) - skipping", ev.request.url, req_id)
                    else:
                        is_main_nav = ev.request.url == main_nav_current_url
                        intercept_states[req_id] = {"phase": "request", "url": ev.request.url, "main_nav": is_main_nav}
                        if is_main_nav:
                            main_nav_request_id = req_id
                        # ask for response interception for everything (simpler) but state machine will gate actions
                        await tab.send(cdp.fetch.continue_request(
                            ev.request_id,
                            headers=[cdp.fetch.HeaderEntry(name=k, value=v) for k,v in request_headers.items()],
                            intercept_response=True,
                        ))
                        intercept_states[req_id]["phase"] = "waiting_response"
                        logger.debug("continued %s request for %s%s", ev.request.method, ev.request.url, " (main_nav)" if is_main_nav else "")
                except Exception as exc:
                    msg = str(exc)
                    if isinstance(exc, nodriver.ProtocolException):
                        logger.warning("request phase race for %s (id=%s): %s", ev.request.url, req_id, msg)
                    else:
                        logger.exception("failed continuing request %s (id=%s)", ev.request.url, req_id)
                finally:
                    async with active_fetch_lock:
                        active_fetch_interceptions.discard(req_id)
                return
            
            response_headers = {h.name.lower(): h.value for h in ev.response_headers}

            logger.debug("intercepted response for %s", ev.request.url)

            mime = response_headers.get("content-type", "").split(";",1)[0].strip().lower()
            scrape_response.intercepted_responses[ev.request.url] = ScrapeResponseIntercepted(
                url=ev.request.url,
                mime=mime,
                headers=response_headers,
                method=ev.request.method,
            )

            # redirect handling for main navigation: update current url + state, never stream redirect bodies
            if (
                ev.response_status_code is not None and 300 <= ev.response_status_code < 400 and
                intercept_states.get(ev.request_id, {}).get("main_nav")
            ):
                loc = response_headers.get("location")
                if loc:
                    try:
                        redirect_url = urljoin(ev.request.url, loc)
                        redirect_chain.append(redirect_url)
                        scrape_response.url = redirect_url  # expose latest
                        logger.info("detected redirect %s -> %s", ev.request.url, redirect_url)
                        # update tracking so subsequent request is considered main nav
                        main_nav_current_url = redirect_url
                        # complete this interception; next request will set new main_nav_request_id
                        state = intercept_states.get(ev.request_id)
                        if state: state["phase"] = "done"
                        await tab.send(cdp.fetch.continue_response(ev.request_id))
                        logger.debug("continued response for redirect %s", ev.request.url)
                    except Exception:
                        logger.exception("failed handling redirect for %s", ev.request.url)
                else:
                    logger.debug("redirect status without location for %s", ev.request.url)
                return

            # we only want the main content
            # and to save memory, we'll skip the bytes if it's just text
            text_types = { "text", "javascript", "json", "xml" }
            if (
                ev.request.url != main_nav_current_url
                or any(t in mime for t in text_types)
            ):
                # continue response (dedupe + handle ProtocolException)
                resp_id = ev.request_id
                async with active_fetch_lock:
                    if resp_id in active_fetch_interceptions:
                        logger.debug("skipping duplicate continue_response for %s (id=%s)", ev.request.url, resp_id)
                        return
                    active_fetch_interceptions.add(resp_id)

                try:
                    state = intercept_states.get(resp_id)
                    if not state or state.get("phase") == "done":
                        logger.debug("stale response phase for %s (id=%s) - skipping", ev.request.url, resp_id)
                        return
                    await tab.send(cdp.fetch.continue_response(ev.request_id))
                    state["phase"] = "done"
                    logger.debug("continued response %s with mime %s", ev.request.url, mime)
                    return
                except Exception as exc:
                    msg = str(exc)
                    if isinstance(exc, nodriver.ProtocolException):
                        logger.debug("response phase race for %s (id=%s): %s", ev.request.url, resp_id, msg)
                        return
                    logger.exception("failed to continue response %s with mime %s", ev.request.url, mime)
                    return
                finally:
                    async with active_fetch_lock:
                        active_fetch_interceptions.discard(resp_id)

            # take the bytes
            logger.info("taking response body as stream for %s", ev.request.url)
            state = intercept_states.get(ev.request_id)
            if state and state.get("phase") == "done":
                logger.debug("already completed interception for %s (id=%s) - skipping body", ev.request.url, ev.request_id)
                return
            stream = await tab.send(cdp.fetch.take_response_body_as_stream(ev.request_id))
            buf = bytearray()
            while True:
                b64, data, eof = await tab.send(cdp.io.read(handle=stream))
                buf.extend(base64.b64decode(data) if b64 else bytes(data, "utf-8"))
                if eof: break
            await tab.send(cdp.io.close(handle=stream))

            # pretend nothing ever happened
            # re-serve it to complete the request
            try:
                await tab.send(
                    cdp.fetch.fulfill_request(ev.request_id,
                        response_code=ev.response_status_code,
                        response_headers=ev.response_headers,
                        body=base64.b64encode(buf).decode(),
                        response_phrase=ev.response_status_text if ev.response_status_text else None,
                    )
                )
                logger.info("successfully fulfilled response %s for %s", ev.request.url, mime)
            except Exception as exc:
                msg = str(exc)
                benign = isinstance(exc, nodriver.ProtocolException) and (
                    "Invalid InterceptionId" in msg or "Invalid state for continueInterceptedRequest" in msg or "Inspected target navigated or closed" in msg
                )
                if benign:
                    logger.debug("benign race fulfilling %s (id=%s): %s", ev.request.url, ev.request_id, exc)
                else:
                    logger.exception("failed to fulfill response %s for %s", ev.request.url, mime)

            # handle 206 chunks if server insists
            cr = response_headers.get("content-range")
            if cr and (m := re.compile(r"bytes (\d+)-(\d+)/(\d+)").match(cr)):
                start, total_len = int(m.group(1)), int(m.group(3))
                chunks.append((start, buf))
                if sum(len(b) for _, b in chunks) < total_len:
                    return
                buf = bytearray(total_len)
                for s, b in chunks: buf[s:s+len(b)] = b

            scrape_response.bytes_ = bytes(buf)
            state = intercept_states.get(ev.request_id)
            if state:
                state["phase"] = "done"
            logger.info("successfully saved bytes for %s", ev.request.url)
            # mark navigation done so we ignore any stray late events
            if intercept_states.get(ev.request_id, {}).get("main_nav"):
                main_nav_done = True

        # wrapper passed to add_handler - schedules the real coroutine as a Task
        def on_fetch(ev: cdp.fetch.RequestPaused):
            task = asyncio.create_task(_on_fetch(ev))
            # register so scrape() can await outstanding tasks before finishing
            pending_tasks.add(task)
            task.add_done_callback(lambda t: pending_tasks.discard(t))
            return None

        tab.add_handler(cdp.fetch.RequestPaused, on_fetch)
        return on_fetch

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
                base = await self.acquire_tab(base=tab if isinstance(tab, nodriver.Tab) else None,
                    new_window=new_window, new_tab=new_tab, new_context=new_window, initial_url="about:blank")
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

    def fix_url(self, url: str):
        """make the url more like how chrome would make it

        :param url: raw url.
        :return: fixed url
        :rtype: str
        """
        return urlcanon.google.canonicalize(url).__str__()


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