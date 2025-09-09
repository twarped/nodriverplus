import asyncio
import json
import re
import os
import logging
import http.server
import socketserver
import threading
import nodriver
from datetime import datetime, UTC
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
                    logger.info("successfully released hanging request for %s (no gate)", self.path)
                except BrokenPipeError:
                    logger.info("client closed connection before immediate release for %s", self.path)
                return

        # append a tuple of (handler, event, path) so the handler can block until release
        event = threading.Event()
        # add without holding the server lock while waiting
        with self.server._lock:
            self.server.active_requests.append((self, event, self.path))

        # block here until release_block sets the event
        logger.info("waiting for gate to release on: %s", self.path)
        event.wait()
        # once released, send a simple OK response and exit
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            logger.info("successfully released hanging request for %s", self.path)
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
        with self._lock:
            for handler, event, p in list(self.active_requests):
                if p == path:
                    # signal the handler to write the response and exit
                    logger.info("successfully found active hanging request for %s; setting...", path)
                    event.set()
                    logger.info("successfully set event to release hanging request for %s", path)
                    self.active_requests.remove((handler, event, p))
                    logger.info("successfully removed hanging request from active requests for %s", path)
                    return True

            # no active handler found; record that this path has been released
            if path not in self.released_paths:
                self.released_paths.add(path)
                logger.info("pre-released hanging request for %s", path)
            else:
                logger.info("no active hanging request found for %s; pre-released", path)
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
        start = datetime.now(UTC)

        logger.info("cloudflare challenge detected on %s; solving...", ev.response.url)
        tab._has_cf_clearance = False

        dark = True
        while tab._has_cf_clearance is False:
            # try both light and dark template images
            await click_template_image(
                tab,
                "cloudflare_dark__x-120__y0.png" if dark else "cloudflare_light__x-120__y0.png",
                flash_point=True,
                save_annotated_screenshot=self.save_annotated_screenshot,
            )
            dark = not dark
            await asyncio.sleep(0.5)
        
        elapsed = (datetime.now(UTC) - start).total_seconds()
        logger.info("solved challenge in %s seconds", elapsed)
        

    async def on_clearance(self,
        ev: cdp.network.ResponseReceived, 
        tab: nodriver.Tab,
    ):
        await tab.reload(ignore_cache=False)
        tab._has_cf_clearance = True

    async def on_request(self, tab, ev, extra_info):
        logger.info("request received: %s <%s>", ev.request_id, ev.request.url)
        # release gate if `request_id` is present in any target's gates.
        # necessary for redirects which will overwrite a gates `request_id`
        # with a new one without releasing the original gate
        for target_id, gates in list(self.gates.items()):
            if ev.request_id in gates:
                path = gates[ev.request_id]
                logger.info("releasing gate for %s redirect on %s", path, ev.request_id)
                self.server.release_block(path)
                gates.pop(ev.request_id, None)
                break
        domain = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        # blobs don't have responses registerable by CDP, so ignore the requests.
        if re.match(rf"^(blob:|http://{domain}/[\w]{{32}})", ev.request.url):
            logger.info("ignoring blob or internal gate request: %s", ev.request.url)
            return

        unique_path = f"/{os.urandom(16).hex()}"
        target_id = tab.target_id
        if target_id not in self.gates:
            self.gates[target_id] = {}
        self.gates[target_id][ev.request_id] = unique_path
        unique_url = f"http://{domain}{unique_path}"
        logger.info("creating gate url %s for %s (origin %s)", unique_url, ev.request_id, ev.request.url)

        await tab.evaluate(f"new Image().src = '{unique_url}';")

    async def on_response(self, tab, ev, extra_info):
        # blobs don't have responses, but just in case
        if re.match(rf"^(blob:|http://{self.server.server_address[0]}:{self.server.server_address[1]}/[\w]{{32}})", ev.response.url):
            return
        headers = ev.response.headers.to_json()
        if extra_info:
            headers = extra_info.headers.to_json()

        cf_chl_gen = headers.get("cf-chl-gen")
        set_cookie = headers.get("set-cookie", "")

        if cf_chl_gen:
            await self.on_detect(ev, tab)
        elif "cf_clearance=" in set_cookie:
            await self.on_clearance(ev, tab)
            
            gates = self.gates[tab.target_id].copy()
            logger.info("releasing gates for %s after clearance", tab)
            for request_id, path in gates.items():
                self.server.release_block(path)
                self.gates[tab.target_id].pop(request_id, None)
        else:
            gate = self.gates.get(tab.target_id, None)
            if not gate:
                logger.info("gates not initiated for %s yet", tab)
                return
            path = gate[ev.request_id]
            if not path:
                logger.info("no gate path found for %s on %s (no challenge header)", ev.request_id, tab)
                return
            logger.info("releasing gate for %s on %s (no challenge header)", path, ev.request_id)
            self.server.release_block(path)
            self.gates[tab.target_id].pop(ev.request_id, None)
