import nodriver
import asyncio
import time
from nodriver import cdp
import logging as logger

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
        fetch_handler = await self._scrape_bytes(
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


async def _scrape_bytes(self, 
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
