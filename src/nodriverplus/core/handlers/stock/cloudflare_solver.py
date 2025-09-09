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
        logger.info("received hanging request for %s", self.path)
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
                logger.info("successfully released hanging request for %s (not gated)", self.path)
            except BrokenPipeError:
                logger.info("client closed connection before immediate release for %s", self.path)
            return

        # block until released by solver
        print("waiting for gate to release on:", self.path)
        event.wait()
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            logger.info("successfully released hanging request for %s (gated)", self.path)
        except BrokenPipeError:
            logger.info("client closed connection before release for %s", self.path)

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
        # try to find an active waiting handler first
        print("getting lock to release", path)
        with self._lock:
            print("lock set, searching for active request")
            for handler, event, p in list(self.active_requests):
                if p == path:
                    print("found active request for", path, "; setting event...")
                    # signal the handler to write the response and exit
                    event.set()
                    print("event set for", path, "; removing from active requests...")
                    self.active_requests.remove((handler, event, p))
                    logger.info("hanging request found and set for %s", path)
                    return True

            # no active handler found; record that this path has been released
            if path not in self.released_paths:
                self.released_paths.add(path)
                logger.info("pre-released hanging path %s (no active request yet)", path)
            else:
                logger.info("release called for already pre-released path %s", path)
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
                                logger.info("background reaper releasing stale gate %s <%s>", request_id, path)
                                self.server.release_block(path)
                                gates.pop(request_id, None)
                                created.pop(request_id, None)
                except Exception as e:
                    logger.error("error in gate reaper: %s", e)
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
            logger.info("releasing stale gate %s <%s>", request_id, path)
            self.server.release_block(path)
            self.gates[target_id].pop(request_id, None)
            created.pop(request_id, None)

    async def on_detect(self,
        ev: cdp.network.ResponseReceived, 
        tab: nodriver.Tab,
    ):
        cdp.network
        logger.info("cloudflare detected on %s", ev.response.url)
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
        logger.info("cloudflare clearance detected on %s", ev.response.url)
        await tab.reload(ignore_cache=False)
        tab._has_cf_clearance = True
        tab._cf_turnstile_detected = False
        logger.info("successfully reload and set _has_cf_clearance on %s", tab.url)

    async def on_request(self, tab, ev, extra_info):
        domain = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        if (re.match(rf"^(blob:|http://{domain}/[\w]{{32}})", ev.request.url) or ev.request.method.lower() != "get"):
            print("ignoring hanging server request", ev.request.url)
            return
        
        print("request id: ", ev.request_id, f"<{ev.request.url}>")
        headers = ev.request.headers.to_json()
        if extra_info:
            headers = extra_info.headers.to_json()

        print("request obj for request id: ", ev.request_id, f"\n{json.dumps(ev.request.to_json(), indent=2)}")
        print("headers for request id: ", ev.request_id, f"\n<{ev.request.url}>", f"\n{json.dumps(headers, indent=2)}")
        current_gates = self.gates.get(tab.target_id, {})
        if current_gates.get(ev.request_id):
            print("redirect detected. releasing gate to allow a new one")
            self.server.release_block(current_gates[ev.request_id])
            self.gates[tab.target_id].pop(ev.request_id, None)
            print("released gate for redirect", ev.request_id)

        unique_path = f"/{os.urandom(16).hex()}"
        target_id = tab.target_id
        if target_id not in self.gates:
            self.gates[target_id] = {}
            self._gate_created[target_id] = {}
        self.gates[target_id][ev.request_id] = unique_path
        self._gate_created[target_id][ev.request_id] = time.time()
        print(json.dumps(self.gates[target_id], indent=2))
        unique_url = f"http://{domain}{unique_path}"
        logger.info("creating gate url %s for %s (origin %s)", ev.request_id, unique_url, ev.request.url)

        await tab.evaluate(f"new Image().src = '{unique_url}';")
        # opportunistically cleanup stale gates
        self._cleanup_stale_gates(tab)

    async def on_loading_failed(self, tab, ev, request_will_be_sent):
        req_url = getattr(getattr(request_will_be_sent, "request", None), "url", "<unknown>")
        print("loading failed id: ", ev.request_id, f"<{req_url}>", f"error: {ev.error_text}")
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
        logger.info("releasing gate after loading failed %s <%s> error=%s", ev.request_id, path, ev.error_text)
        self.server.release_block(path)
        gates.pop(ev.request_id, None)
        self._gate_created.get(target_id, {}).pop(ev.request_id, None)
        self._cleanup_stale_gates(tab)

    async def on_response(self, tab, ev, extra_info):
        if re.match(rf"^(blob:|http://{self.server.server_address[0]}:{self.server.server_address[1]}/[\w]{{32}})", ev.response.url):
            print("ignoring hanging server response", ev.response.url)
            return
        
        print("response id: ", ev.request_id, f"<{ev.response.url}>")
        print("response obj for response id: ", ev.request_id, f"\n{json.dumps(ev.response.to_json(), indent=2)}")
        headers = ev.response.headers.to_json()
        if extra_info:
            headers = extra_info.headers.to_json()
        print("headers for response id:", ev.request_id, f"\n<{ev.response.url}>", f"\n{json.dumps(headers, indent=2)}")

        cf_ray = headers.get("cf-ray")
        cf_chl_gen = headers.get("cf-chl-gen")
        set_cookie = headers.get("set-cookie", "")
        cookies: list[cdp.network.Cookie] = await tab.send(cdp.network.get_cookies([tab.url]))
        cookies_list = [c.name for c in cookies]
        print(f"current cookies for {ev.request_id} on <{tab.url}>:\n {cookies_list}")
        turnstile = re.match(r".*\/turnstile\/v0(\/.*)?\/api\.js*", ev.response.url)

        print(cf_ray, turnstile)
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
            
            gates = self.gates[tab.target_id].copy()
            for request_id, path in gates.items():
                print("releasing gate", path, f"<{request_id}>")
                self.server.release_block(path)
                self.gates[tab.target_id].pop(request_id, None)
            print("cleared all gates for target ", tab.target_id)
            print("current gates after clearance:", json.dumps(self.gates, indent=2))
        else:
            print(json.dumps(self.gates, indent=2))
            try:
                path = self.gates[tab.target_id][ev.request_id]
            except KeyError:
                print("no gate found for response", ev.request_id, f"<{ev.response.url}>")
                return
            print("releasing gate for response", ev.request_id, f"<{path}>")
            self.server.release_block(path)
            print("released gate for response", ev.request_id, f"<{path}>")
            self.gates[tab.target_id].pop(ev.request_id, None)
            self._gate_created.get(tab.target_id, {}).pop(ev.request_id, None)
        # cleanup stale after each response
        self._cleanup_stale_gates(tab)
