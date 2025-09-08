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
    def do_GET(self):
        logger.info("received hanging request for %s", self.path)
        # if this path was already released before the request arrived, respond
        # immediately. use the server lock for thread-safety.
        with getattr(self.server, "_lock", threading.Lock()):
            if getattr(self.server, "released_paths", None) and self.path in self.server.released_paths:
                try:
                    # consume the released marker for this path and respond
                    try:
                        self.server.released_paths.remove(self.path)
                    except KeyError:
                        pass
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")
                except BrokenPipeError:
                    logger.info("client closed connection before immediate release for %s", self.path)
                return

        # append a tuple of (handler, event, path) so the handler can block until release
        event = threading.Event()
        # add without holding the server lock while waiting
        with self.server._lock:
            self.server.active_requests.append((self, event, self.path))

        # block here until release_block sets the event
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
        with self._lock:
            for handler, event, p in list(self.active_requests):
                if p == path:
                    # signal the handler to write the response and exit
                    event.set()
                    try:
                        self.active_requests.remove((handler, event, p))
                    except ValueError:
                        pass
                    logger.debug("released hanging request for %s", path)
                    return True

            # no active handler found; record that this path has been released
            if path not in self.released_paths:
                self.released_paths.add(path)
                logger.debug("pre-released hanging path %s (no active request yet)", path)
            else:
                logger.debug("release called for already pre-released path %s", path)
            return False

class CloudflareSolver(NetworkWatcher):
    """per-tab hanging gate manager for cloudflare challenges.

    design:
    - every request before clearance creates a unique hanging path (gate)
    - gates stay blocked while responses carry `cf-chl-gen` (challenge active)
    - responses without `cf-chl-gen` release only their own gate immediately
    - first response setting `cf_clearance` marks that tab cleared and releases *all* its gates
    - other tabs remain blocked until they independently obtain clearance
    - no global fallbacks / bulk releases unrelated to the owning tab
    """

    def __init__(self,
        save_annotated_screenshot: str | os.PathLike = None,
        hanging_server_port: int = 0
    ):
        self.save_annotated_screenshot = save_annotated_screenshot
        # bind explicitly to IPv4 loopback
        self.server = HangingServer(("127.0.0.1", hanging_server_port), HangingHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # per-tab state: target_id -> { cleared: bool, req_to_path: {req_id: path}, outstanding: set[path] }
        self._tabs: dict[str, dict[str, object]] = {}

    # internal helper
    def _state(self, tab: nodriver.Tab):
        st = self._tabs.get(tab.target_id)
        if st is None:
            st = {"cleared": False, "req_to_path": {}, "outstanding": set()}
            self._tabs[tab.target_id] = st
        return st

    async def on_detect(self,
        ev: cdp.network.ResponseReceived, 
        tab: nodriver.Tab,
    ):
        logger.info("cloudflare detected on %s", ev.response.url)

        while not hasattr(tab, "_has_cf_clearance"):
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
        st = self._state(tab)
        if re.match(rf"https?://{re.escape(self.server.server_address[0])}:{self.server.server_address[1]}/", ev.request.url):
            return
        # create a unique gate path for this request id
        unique_path = f"/{os.urandom(16).hex()}"
        unique_url = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}{unique_path}"
        st["req_to_path"][ev.request_id] = unique_path  # type: ignore[index]
        st["outstanding"].add(unique_path)  # type: ignore[union-attr]
        logger.info("creating gate path %s for request %s (%s)", unique_path, ev.request_id, ev.request.url)
        # persist the image so it isn't GC'd before dispatch
        await tab.evaluate(f"new Image().src = {unique_url};")

    async def on_response(self, tab, ev, extra_info):
        st = self._state(tab)
        headers = ev.response.headers.to_json()
        if extra_info:
            headers = extra_info.headers.to_json()

        cf_chl_gen = headers.get("cf-chl-gen")
        set_cookie = headers.get("set-cookie", "")

        path = st["req_to_path"].pop(ev.request_id, None)  # type: ignore[index]

        if cf_chl_gen:
            # challenge still active; keep gate hanging (do not release)
            if path:
                logger.debug("keeping gate %s (challenge header present) for %s", path, ev.response.url)
            await self.on_detect(ev, tab)
            return

        if "cf_clearance=" in set_cookie:
            # mark clearance and release all gates for this tab only
            if not st["cleared"]:
                await self.on_clearance(ev, tab)
                st["cleared"] = True
            # release current path first (if any)
            if path and path in st["outstanding"]:
                logger.info("releasing gate %s (clearance) for %s", path, ev.response.url)
                st["outstanding"].discard(path)
                self.server.release_block(path)
            # release all remaining outstanding gates for this tab
            for p in list(st["outstanding"]):
                logger.info("releasing gate %s (bulk clearance) for tab %s", p, tab.target_id)
                st["outstanding"].discard(p)
                self.server.release_block(p)
            return

        # no challenge header and no clearance cookie => allow this single gate to pass
        if path:
            if path in st["outstanding"]:
                logger.info("releasing gate %s (no challenge header) for %s", path, ev.response.url)
                st["outstanding"].discard(path)
                self.server.release_block(path)
        return
