import asyncio
import json
import re
import os
import logging
import http.server
import socketserver
import threading
import time
import nodriver
from nodriver import cdp
from ..target_intercepted import NetworkWatcher
from ...tab import click_template_image

logger = logging.getLogger(__name__)

class HangingHandler(http.server.BaseHTTPRequestHandler):
    server: "HangingServer"
    def do_GET(self):
        logger.debug("hanging request %s", self.path)
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
                logger.info("released hanging request %s (immediate)", self.path)
            except BrokenPipeError:
                logger.debug("client closed before immediate release %s", self.path)
            return

        event.wait()
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            logger.info("released hanging request %s", self.path)
        except BrokenPipeError:
            logger.debug("client closed before gated release %s", self.path)

    def log_message(self, format, *args):
        # silence logs
        return

class HangingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
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

    def release_block(self, path: str):
        with self._lock:
            for handler, event, p in list(self.active_requests):
                if p == path:
                    event.set()
                    self.active_requests.remove((handler, event, p))
                    logger.debug("signalled hanging request %s", path)
                    return True
            if path not in self.released_paths:
                self.released_paths.add(path)
                logger.debug("pre-released path %s", path)
            return False

class CloudflareSolver(NetworkWatcher):
    """
    stock `ResponseReceivedHandler` for detecting Cloudflare protection
    """

    def __init__(self, 
        save_annotated_screenshot: str | os.PathLike = None, 
        hanging_server_port: int = 0
    ):
        self.save_annotated_screenshot = save_annotated_screenshot
        # bind explicitly to the IPv4 loopback so the browser connects to the same
        # address family the server listens on (avoids ::1 vs 127.0.0.1 mismatch)
        self.server = HangingServer(("127.0.0.1", hanging_server_port), HangingHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.gates: dict[cdp.target.TargetID, dict[str, str]] = {}
        # track gate creation times to reap stale ones
        self._gate_created: dict[cdp.target.TargetID, dict[str, float]] = {}
        # start background reaper to ensure gates are eventually released even if traffic stops
        self._start_reaper()

    def _start_reaper(self, interval: float = 5.0, max_age: float = 30.0):
        def loop():
            while True:
                try:
                    now = time.time()
                    for target_id, gates in list(self.gates.items()):
                        created = self._gate_created.get(target_id, {})
                        for request_id, path in list(gates.items()):
                            ts = created.get(request_id)
                            if ts and now - ts > max_age:
                                logger.debug("background reaper releasing stale gate %s <%s>", request_id, path)
                                self.server.release_block(path)
                                gates.pop(request_id, None)
                                created.pop(request_id, None)
                except Exception:
                    logger.exception("error in gate reaper: %s")
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True).start()

    def _cleanup_stale_gates(self, tab: nodriver.Tab, max_age: float = 30.0):
        # release any gates older than max_age seconds
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
            logger.debug("releasing stale gate %s <%s>", request_id, path)
            self.server.release_block(path)
            self.gates[target_id].pop(request_id, None)
            created.pop(request_id, None)

    async def on_detect(self,
        ev: cdp.network.ResponseReceived, 
        tab: nodriver.Tab,
    ):
        cdp.network
        logger.info("cloudflare challenge detected %s", ev.response.url)
        tab._has_cf_clearance = False
        tab._cf_turnstile_detected = True

        while tab._has_cf_clearance is False:
            # try both light and dark template images
            for template in [
                "cloudflare_light__x-120__y0.png",
                "cloudflare_dark__x-120__y0.png",
            ]:
                await click_template_image(
                    tab,
                    template,
                    flash_point=True,
                    save_annotated_screenshot=self.save_annotated_screenshot,
                )
                await asyncio.sleep(0.5)

    async def on_clearance(self,
        ev: cdp.network.ResponseReceived, 
        tab: nodriver.Tab,
    ):
        logger.info("cloudflare clearance %s", ev.response.url)
        await tab.reload(ignore_cache=False)
        tab._has_cf_clearance = True
        tab._cf_turnstile_detected = False
        logger.info("successfully reload and set _has_cf_clearance on %s", tab.url)

    async def on_request(self, tab, ev, extra_info):
        domain = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        if (re.match(rf"^(blob:|http://{domain}/[\w]{{32}})", ev.request.url) or ev.request.method.lower() != "get"):
            logger.debug("ignore request %s", ev.request.url)
            return
        current_gates = self.gates.get(tab.target_id, {})
        if current_gates.get(ev.request_id):
            self.server.release_block(current_gates[ev.request_id])
            self.gates[tab.target_id].pop(ev.request_id, None)
            logger.debug("released gate for redirect %s", ev.request_id)

        unique_path = f"/{os.urandom(16).hex()}"
        target_id = tab.target_id
        if target_id not in self.gates:
            self.gates[target_id] = {}
            self._gate_created[target_id] = {}
        self.gates[target_id][ev.request_id] = unique_path
        self._gate_created[target_id][ev.request_id] = time.time()
        unique_url = f"http://{domain}{unique_path}"
        logger.debug("gate %s -> %s origin=%s", ev.request_id, unique_url, ev.request.url)

        await tab.evaluate(f"new Image().src = '{unique_url}';")
        # opportunistically cleanup stale gates
        self._cleanup_stale_gates(tab)

    async def on_loading_failed(self, tab, ev, request_will_be_sent):
        req_url = getattr(getattr(request_will_be_sent, "request", None), "url", "<unknown>")
        logger.debug("loading failed %s %s err=%s", ev.request_id, req_url, ev.error_text)
        # release gate if a gated request failed
        target_id = tab.target_id
        gates = self.gates.get(target_id)
        if not gates:
            logger.info("no gates found for %s", tab)
            return
        path = gates.get(ev.request_id)
        if not path:
            logger.info("no gate found for failed request %s <%s>", ev.request_id, req_url)
            return
        logger.debug("releasing gate after loading failed %s <%s> error=%s", ev.request_id, path, ev.error_text)
        self.server.release_block(path)
        gates.pop(ev.request_id, None)
        self._gate_created.get(target_id, {}).pop(ev.request_id, None)
        self._cleanup_stale_gates(tab)

    async def on_response(self, tab, ev, extra_info):
        if re.match(rf"^(blob:|http://{self.server.server_address[0]}:{self.server.server_address[1]}/[\w]{{32}})", ev.response.url):
            logger.debug("ignore hanging response %s", ev.response.url)
            return
        headers = extra_info.headers.to_json() if extra_info else ev.response.headers.to_json()

        cf_ray = headers.get("cf-ray")
        cf_chl_gen = headers.get("cf-chl-gen")
        set_cookie = headers.get("set-cookie", "")
        cookies: list[cdp.network.Cookie] = await tab.send(cdp.network.get_cookies([tab.url]))
        cookies_list = [c.name for c in cookies]
        logger.debug("cookies(%s) %s", ev.request_id, cookies_list)
        turnstile = re.match(r".*\/turnstile\/v0(\/.*)?\/api\.js*", ev.response.url)

        logger.debug("cf headers ray=%s turnstile=%s", cf_ray, bool(turnstile))
        if cf_chl_gen:
            if "cf_clearance" in cookies_list:
                await tab.send(cdp.network.delete_cookies(
                    name="cf_clearance", 
                    domain=cookies[cookies_list.index("cf_clearance")].domain,
                ))
            if not getattr(tab, "_cf_turnstile_detected", False):
                await self.on_detect(ev, tab)
        elif (
            cf_ray and turnstile
            and not getattr(tab, "_cf_turnstile_detected", False)
            and not "cf_clearance" in cookies_list
        ):
            await self.on_detect(ev, tab)
        elif "cf_clearance=" in set_cookie:
            await self.on_clearance(ev, tab)
            
            gates = self.gates.get(tab.target_id, {}).copy()
            for request_id, path in gates.items():
                self.server.release_block(path)
                self.gates[tab.target_id].pop(request_id, None)
            logger.debug("cleared gates target %s", tab.target_id)
        else:
            try:
                path = self.gates[tab.target_id][ev.request_id]
            except KeyError:
                logger.debug("no gate found for response %s %s", ev.request_id, ev.response.url)
                return
            self.server.release_block(path)
            logger.debug("released gate for response %s %s", ev.request_id, path)
            self.gates[tab.target_id].pop(ev.request_id, None)
            self._gate_created.get(tab.target_id, {}).pop(ev.request_id, None)
        # cleanup stale after each response
        self._cleanup_stale_gates(tab)
