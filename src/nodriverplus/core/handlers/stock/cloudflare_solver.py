import asyncio
import json
import re
import os
import logging
import http.server
import socketserver
import threading
import nodriver
from nodriver import cdp
from ..target_intercepted import NetworkWatcher
from ...tab import click_template_image

logger = logging.getLogger(__name__)

class HangingHandler(http.server.BaseHTTPRequestHandler):
    server: "HangingServer"
    def do_GET(self):
        logger.info("received hanging request for %s", self.path)
        # if this path was already released before the request arrived, respond
        # immediately. use the server lock for thread-safety.
        with getattr(self.server, "_lock", threading.Lock()):
            if getattr(self.server, "released_paths", None) and self.path in self.server.released_paths:
                try:
                    # consume the released marker for this path and respond
                    self.server.released_paths.remove(self.path)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")
                    logging.info("immediately released hanging request for %s", self.path)
                except BrokenPipeError:
                    logger.info("client closed connection before immediate release for %s", self.path)
                return

        # append a tuple of (handler, event, path) so the handler can block until release
        event = threading.Event()
        # add without holding the server lock while waiting
        with self.server._lock:
            self.server.active_requests.append((self, event, self.path))

        # block here until release_block sets the event
        print("waiting for gate to release on:", self.path)
        event.wait()
        # once released, send a simple OK response and exit
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            logger.info("released hanging request for %s", self.path)
        except BrokenPipeError:
            # client already closed connection - nothing to do
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

    async def on_detect(self,
        ev: cdp.network.ResponseReceived, 
        tab: nodriver.Tab,
    ):
        logger.info("cloudflare detected on %s", ev.response.url)
        tab._has_cf_clearance = False

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
        logger.info("successfully reload and set _has_cf_clearance on %s", tab.url)

    async def on_request(self, tab, ev, extra_info):
        domain = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        if re.match(rf"^(blob:|http://{domain}/[\w]{{32}})", ev.request.url):
            print("ignoring hanging server request", ev.request.url)
            return
        
        print("request id: ", ev.request_id, f"<{ev.request.url}>")
        headers = ev.request.headers.to_json()
        if extra_info:
            headers = extra_info.headers.to_json()
        print("headers for request id: ", ev.request_id, f"\n{json.dumps(headers, indent=2)}")
        current_gates = self.gates.get(tab.target_id, {})
        if current_gates.get(ev.request_id):
            print("redirect detected. releasing gate to allow a new one")
            self.server.release_block(current_gates[ev.request_id])
            self.gates[tab.target_id].pop(ev.request_id, None)
            print("successfully released gate for redirect", ev.request_id)

        unique_path = f"/{os.urandom(16).hex()}"
        target_id = tab.target_id
        if target_id not in self.gates:
            self.gates[target_id] = {}
        self.gates[target_id][ev.request_id] = unique_path
        print(json.dumps(self.gates[target_id], indent=2))
        unique_url = f"http://{domain}{unique_path}"
        logger.info("creating gate url %s for %s (origin %s)", ev.request_id, unique_url, ev.request.url)

        await tab.evaluate(f"new Image().src = '{unique_url}';")

    async def on_response(self, tab, ev, extra_info):
        if re.match(rf"^(blob:|http://{self.server.server_address[0]}:{self.server.server_address[1]}/[\w]{{32}})", ev.response.url):
            print("ignoring hanging server response", ev.response.url)
            return
        
        print("response id: ", ev.request_id, f"<{ev.response.url}>")
        headers = ev.response.headers.to_json()
        if extra_info:
            headers = extra_info.headers.to_json()
        print(json.dumps(headers, indent=2))

        cf_chl_gen = headers.get("cf-chl-gen")
        set_cookie = headers.get("set-cookie", "")

        if cf_chl_gen:
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
            path = self.gates[tab.target_id][ev.request_id]
            print("releasing gate for response", ev.request_id, f"<{path}>")
            self.server.release_block(path)
            print("successfully released gate for response", ev.request_id, f"<{path}>")
            self.gates[tab.target_id].pop(ev.request_id, None)
