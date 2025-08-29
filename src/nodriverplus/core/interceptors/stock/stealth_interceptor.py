from nodriver import cdp
import nodriver
import logging
from ..target_interceptors import TargetInterceptor
from ....js.load import load_text as load_js
from ...cdp_helpers import can_use_domain
from ...connection import send_cdp

logger = logging.getLogger(__name__)

class StealthInterceptor(TargetInterceptor):

    async def apply_stealth(connection, ev: cdp.target.AttachedToTarget):
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
                await send_cdp(connection, "Page.enable", session_id=ev.session_id)
                await send_cdp(connection, "Page.addScriptToEvaluateOnNewDocument", {
                    "source": js,
                    "includeCommandLineAPI": True,
                    "runImmediately": True
                }, ev.session_id)
                logger.info("successfully added script to %s", msg)
        except Exception:
            logger.exception("failed to add script to %s:", msg)

        try:
            await send_cdp("Runtime.evaluate", {
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
            logger.info("successfully applied patch to %s", msg)

    async def handle(self, connection: nodriver.Tab | nodriver.Connection, ev: cdp.target.AttachedToTarget):
        await self.apply_stealth(connection, ev)