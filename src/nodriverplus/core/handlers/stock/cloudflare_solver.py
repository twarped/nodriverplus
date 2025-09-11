"""
**TODO**:

- either move HangingServer to it's own API
- or turn this into a generic `CaptchaSolver` that can be used
to build custom captcha solvers, and wire `CloudflareSolver` to
use the new `CaptchaSolver` API.
"""

import asyncio
import re
import os
import sys
import logging
import http.server
import socketserver
import threading
import time
import nodriver
from nodriver import cdp
from ..target_intercepted import NetworkWatcher
from ...tab import click_template_image

logger = logging.getLogger("nodriverplus.CloudflareSolver")

class HangingHandler(http.server.BaseHTTPRequestHandler):
    """request handler for `HangingServer`
    """
    server: "HangingServer"
    def do_GET(self):
        """where the magic happens.

        this method will block the request until the path
        is released via `HangingServer.release_block()`
        """
        logger.debug("received hanging request: %s", self.path)
        # single critical section to avoid race where release happens between initial check and append
        event = threading.Event()
        immediate = False
        with self.server._lock:
            if self.path in self.server.released_paths:
                # was pre-released before arrival
                self.server.released_paths.remove(self.path)
                immediate = True
            else:
                # register active waiter
                self.server.active_requests.append((self, event, self.path))

        if immediate:
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
                logger.debug("released hanging request: %s (immediate)", self.path)
            except BrokenPipeError:
                logger.debug("client closed before immediate release (BrokenPipeError): %s", self.path)
            except ConnectionAbortedError:
                logger.debug("client closed before immediate release (ConnectionAbortedError): %s", self.path)
            except ConnectionResetError:
                logger.debug("client closed before immediate release (ConnectionResetError): %s", self.path)
            return

        event.wait()
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            logger.debug("released hanging request: %s", self.path)
        except BrokenPipeError:
            logger.debug("client closed before gated release (BrokenPipeError): %s", self.path)
        except ConnectionAbortedError:
            logger.debug("client closed before gated release (ConnectionAbortedError): %s", self.path)
        except ConnectionResetError:
            logger.debug("client closed before gated release (ConnectionResetError): %s", self.path)

    def log_message(self, format, *args):
        # silence logs
        return

class HangingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """simple HTTP server that hangs requests until explicitly released
    
    initially built for `CloudflareSolver`
    """
    # prefer immediate re-use during development
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # list of tuples: (handler, event, path)
        self.active_requests: list[tuple[HangingHandler, threading.Event, str]] = []
        # set of paths that have been released before a request arrived
        self.released_paths: set[str] = set()
        # protect active_requests and released_paths
        self._lock = threading.Lock()

    def handle_error(self, request, client_address):
        """overrides default error handling to quietly ignore benign
        `ConnectionResetError` instances that happen during shutdown.

        this prevents noisy tracebacks like `"ConnectionResetError: An
        existing connection was forcibly closed by the remote host"` from
        being printed to stderr while still delegating other exceptions to
        the base implementation.
        """

        exc_type, exc_value, _ = sys.exc_info()
        if isinstance(exc_value, ConnectionResetError):
            logger.debug("ignored ConnectionResetError from %s", client_address)
            return
        # fallback to default for anything else
        return super().handle_error(request, client_address)

    def release_block(self, path: str):
        """release a hanging request if it exists, otherwise pre-release the path

        :param path: the path to release, e.g. "/abcdef1234567890"
        """
        with self._lock:
            for handler, event, p in list(self.active_requests):
                if p == path:
                    event.set()
                    self.active_requests.remove((handler, event, p))
                    logger.debug("signalled release of hanging request: %s", path)
                    return True
            if path not in self.released_paths:
                self.released_paths.add(path)
                logger.debug("pre-released hanging request: %s", path)
            return False

class CloudflareSolver(NetworkWatcher):
    """
    stock `NetworkWatcher` for detecting and solving Cloudflare challenges

    this handler works by detecting cloudflare challenge responses
    and then injecting an image into the page that will hang while
    it clicks the checkbox until `cf_clearance` is obtained.

    hanging images causes a page to not finish loading, which
    prevents a scrape from finishing until the challenge is solved.

    **TODO**:
    - this handler cannot detect login based turnstile challenges yet,
    only generic checkbox challenges.
    - move HangingServer to it's own API that users can easily use to:
        - specify when and if to hang a site so it can't finish loading
        - do stuff in the middle
        - specify when to release the site hang
        - and also work along side the stock `CloudflareSolver` handler
    """

    def __init__(self, 
        save_annotated_screenshot: str | os.PathLike = None,
        flash_point: bool = False,
        hanging_server_port: int = 0
    ):
        """initialize the `CloudflareSolver`

        :param save_annotated_screenshot: if set, saves an annotated screenshot
        to the given path when the iframe is found. useful for debugging.
        :param flash_point: if True, flashes the click point on the page. defaults
        to `False` because the flash messes up the screenshot given to `opencv`.
        :param hanging_server_port: port for the internal `HangingServer` to listen on.
        defaults to `0` which means a random free port will be chosen.
        """
        self.save_annotated_screenshot = save_annotated_screenshot
        self.flash_point = flash_point
        # bind explicitly to the IPv4 loopback so the browser connects to the same
        # address family the server listens on (avoids ::1 vs 127.0.0.1 mismatch)
        self.server = HangingServer(("127.0.0.1", hanging_server_port), HangingHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        self.gates: dict[cdp.target.TargetID, dict[str, str]] = {}
        # track gate creation times to reap stale ones
        self._gate_created: dict[cdp.target.TargetID, dict[str, float]] = {}
        # signal to stop background reaper
        self._stop_reaper = threading.Event()
        # start background reaper to ensure gates are eventually released even if traffic stops
        self._start_reaper()

    async def stop(self):
        """
        stop `CloudflareSolver` and all background threads/servers
        """
        # print("stopping the solver")
        # signal background thread to exit
        self._stop_reaper.set()
        # stop server so serve_forever returns
        try:
            self.server.shutdown()
        except Exception:
            logger.exception("error during server.shutdown()")
        # wait for threads to finish (no timeout to ensure clean exit)
        if self.reaper_thread.is_alive():
            self.reaper_thread.join()
        if self.server_thread.is_alive():
            self.server_thread.join()
        # print("stopped the solver")

    def _start_reaper(self, interval: float = 5.0, max_age: float = 30.0):
        """start background thread to reap stale gates"""
        def loop():
            while not self._stop_reaper.is_set():
                try:
                    now = time.time()
                    for target_id, gates in list(self.gates.items()):
                        created = self._gate_created.get(target_id, {})
                        for request_id, path in list(gates.items()):
                            ts = created.get(request_id)
                            if ts and now - ts > max_age:
                                logger.debug("reaper releasing stale gate %s -> %s", request_id, path)
                                self.server.release_block(path)
                                gates.pop(request_id, None)
                                created.pop(request_id, None)
                except Exception:
                    logger.exception("error in gate reaper")
                # wait with wake-up on stop
                self._stop_reaper.wait(interval)
        self.reaper_thread = threading.Thread(target=loop, daemon=True)
        self.reaper_thread.start()

    def _cleanup_stale_gates(self, tab: nodriver.Tab, max_age: float = 4.0):
        """release any gates older than max_age seconds
        
        :param tab: the `Tab` whose gates to clean up
        :param max_age: the maximum age of a gate in seconds before it is considered stale
        """
        target_id = tab.target_id
        created = self._gate_created.get(target_id)
        if not created:
            return
        now = time.time()
        stale: list[tuple[str, str]] = []
        for request_id, ts in list(created.items()):
            if now - ts > max_age and request_id in self.gates.get(target_id, {}):
                path = self.gates[target_id][request_id]
                stale.append((request_id, path))
        for request_id, path in stale:
            logger.debug("releasing stale gate %s -> %s", request_id, path)
            self.server.release_block(path)
            self.gates[target_id].pop(request_id, None)
            created.pop(request_id, None)

    async def on_detect(self,
        tab: nodriver.Tab,
    ):
        """called when a cloudflare challenge is detected
        
        :param tab: the `Tab` where the challenge was detected
        """
        logger.info("cloudflare challenge detected on %s", tab)
        tab._has_cf_clearance = False
        tab._cf_turnstile_detected = True
        # memo browser tabs list for quick lookups (avoid O(n) every loop)
        def tab_alive() -> bool:
            return any(t.target_id == tab.target_id for t in tab.browser.tabs)

        # try both light and dark template images
        base_dir = os.path.dirname(os.path.abspath(__file__))
        templates = [
            os.path.join(base_dir, "cloudflare_light__x-120__y0.png"),
            os.path.join(base_dir, "cloudflare_dark__x-120__y0.png"),
        ]
        while (not tab._has_cf_clearance
               and not self._stop_reaper.is_set()
               and tab_alive()):
            
            for template in templates:
                if tab._has_cf_clearance:
                    break
                if not tab_alive():
                    logger.debug("tab detached during cloudflare solve; aborting loop")
                    break
                await click_template_image(
                    tab,
                    template,
                    flash_point=self.flash_point,
                    save_annotated_screenshot=self.save_annotated_screenshot,
                )
            await asyncio.sleep(0.5)

    async def on_clearance(self,
        tab: nodriver.Tab,
    ):
        """called when cloudflare clearance is detected
        
        :param tab: the `Tab` where the clearance was detected
        """
        logger.debug("clearance detected on %s", tab)
        await tab.reload()
        tab._has_cf_clearance = True
        tab._cf_turnstile_detected = False
        logger.info("successfully solved cloudflare challenge for %s", tab)

    def should_ignore(self, url, method="get"):
        """whether to ignore a request/response for gating
        
        :param url: the request/response URL
        :param method: the HTTP method if specified, defaults to "get"
        """
        address = self.server.server_address
        return (
            re.match(rf"^(blob:|data:|http://{address[0]}:{address[1]}/[\w]{{32}})", url)
            or method.lower() != "get"
        )

    async def on_request(self, tab, ev, extra_info):
        """called on each `RequestWillBeSent` event
        
        sets up hang gates for requests that shouldn't be ignored
        """
        if self.should_ignore(ev.request.url, ev.request.method):
            logger.debug("ignoring request: %s", ev.request.url)
            return
        current_gates = self.gates.get(tab.target_id, {})
        if current_gates.get(ev.request_id):
            self.server.release_block(current_gates[ev.request_id])
            self.gates[tab.target_id].pop(ev.request_id, None)
            logger.debug("released gate for redirect: %s", ev.request_id)

        unique_path = f"/{os.urandom(16).hex()}"
        target_id = tab.target_id
        if target_id not in self.gates:
            self.gates[target_id] = {}
            self._gate_created[target_id] = {}
        self.gates[target_id][ev.request_id] = unique_path
        self._gate_created[target_id][ev.request_id] = time.time()
        unique_url = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}{unique_path}"
        logger.debug("gate %s -> %s (origin=%s)", ev.request_id, unique_url, ev.request.url)

        await tab.evaluate(f"new Image().src = '{unique_url}';")
        # opportunistically cleanup stale gates
        self._cleanup_stale_gates(tab)

    async def on_loading_failed(self, tab, ev, request_will_be_sent):
        """called on each `LoadingFailed` event

        releases gates on failed requests/responses
        so that they don't hang forever unknown        
        """
        req_url = request_will_be_sent.request.url if request_will_be_sent else "unknown"
        logger.debug("loading failed req=%s url=%s err=%s", ev.request_id, req_url, ev.error_text)
        # release gate if a gated request failed
        target_id = tab.target_id
        gates = self.gates.get(target_id)
        if not gates:
            logger.debug("no gates found for %s", tab)
            return
        path = gates.get(ev.request_id)
        if not path:
            # no gate was created for this request id
            logger.debug("no gate for failed request %s <%s>", ev.request_id, req_url)
            return

        # signal the hanging handler and remove gate/state
        try:
            self.server.release_block(path)
        except Exception:
            logger.exception("error releasing gate for failed request %s -> %s", ev.request_id, path)

        gates.pop(ev.request_id, None)
        # also clear creation timestamp if present
        self._gate_created.get(target_id, {}).pop(ev.request_id, None)
        logger.debug("releasing gate after loading failed %s <%s> err=%s", ev.request_id, path, ev.error_text)
        self._cleanup_stale_gates(tab)

    async def on_response(self, tab, ev, extra_info):
        """called on each `ResponseReceived` event

        handles cloudflare challenge detection and clearance

        ### flow:
        1. ignore irrelevant responses
        ### then:
        2. check response headers for the `cf-chl-gen` header
        3. fallback check:
            1. first check if the response has a `cf-ray` header
            2. then, if the URL is a turnstile script:
            3. check if we have a `cf_clearance` cookie.
            4. if there's no `cf_clearance` cookie, and the URL 
            matches the turnstile pattern, then assume a checkbox challenge.
        4. if `2` or `3.4` is true, then solve the challenge in `on_detect()`
        ### otherwise:
        5. if we have instead received a `cf_clearance` cookie, 
        then signal that the challenge is solved with 
        `on_clearance()` and release gates.
        ### or:
        (we already checked this in `2`, but it 
        makes more sense reading it like this:)

        6. if the response has a `cf-chl-gen` header,
        then delete any existing `cf_clearance` cookie
        so that step `3` hopefully won't have any false positives.
        """
        if self.should_ignore(ev.response.url):
            logger.debug("ignoring response: %s", ev.response.url)
            return
        headers = extra_info.headers.to_json() if extra_info else ev.response.headers.to_json()

        cf_ray = headers.get("cf-ray")
        cf_chl_gen = headers.get("cf-chl-gen")
        set_cookie = headers.get("set-cookie", "")
        cookies: list[cdp.network.Cookie] = await tab.send(cdp.network.get_cookies([tab.url]))
        cookies_list = [c.name for c in cookies]
        logger.debug("cookies(%s) %s", ev.request_id, cookies_list)
        turnstile = re.match(r".*\/turnstile\/v0(\/.*)?\/api\.js*", ev.response.url)

        logger.debug("headers ray=%s turnstile=%s", cf_ray, bool(turnstile))
        if cf_chl_gen:
            if "cf_clearance" in cookies_list:
                await tab.send(cdp.network.delete_cookies(
                    name="cf_clearance", 
                    domain=cookies[cookies_list.index("cf_clearance")].domain,
                ))
            if not getattr(tab, "_cf_turnstile_detected", False):
                await self.on_detect(tab)
        elif (
            cf_ray and turnstile
            and not getattr(tab, "_cf_turnstile_detected", False)
            and not "cf_clearance" in cookies_list
        ):
            await self.on_detect(tab)
        elif "cf_clearance=" in set_cookie:
            await self.on_clearance(tab)

            gates = self.gates.get(tab.target_id, {}).copy()
            for request_id, path in gates.items():
                self.server.release_block(path)
                self.gates[tab.target_id].pop(request_id, None)
            logger.debug("cleared gates for target %s", tab.target_id)
        else:
            try:
                path = self.gates[tab.target_id][ev.request_id]
            except KeyError:
                logger.debug("no gate for response %s %s", ev.request_id, ev.response.url)
                return
            self.server.release_block(path)
            logger.debug("released gate for response %s -> %s", ev.request_id, path)
            self.gates[tab.target_id].pop(ev.request_id, None)
            self._gate_created.get(tab.target_id, {}).pop(ev.request_id, None)
        # cleanup stale after each response
        self._cleanup_stale_gates(tab)
