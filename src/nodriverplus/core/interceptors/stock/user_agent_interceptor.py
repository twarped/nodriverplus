import logging
import nodriver
from nodriver import cdp
from ..target_interceptors import TargetInterceptor
from ...connection import send_cdp
from ...user_agent import UserAgent
from ...cdp_helpers import can_use_domain
from ....js.load import load_text as load_js

logger = logging.getLogger(__name__)

class UserAgentInterceptor(TargetInterceptor):
    user_agent: UserAgent
    stealth: bool

    def __init__(self, user_agent: UserAgent = None, stealth: bool = False):
        self.user_agent = user_agent
        self.stealth = stealth

    async def patch_user_agent(self, 
        connection: nodriver.Tab | nodriver.Connection,
        ev: cdp.target.AttachedToTarget,
    ):
        """apply UA overrides across relevant domains for a target.

        removes "Headless" when `stealth=True`

        sets Network + Emulation overrides and installs a runtime 
        patch so navigator + related surfaces align. worker/page aware.

        :param target: tab or AttachedToTarget event.
        :param user_agent: prepared UserAgent instance.
        """
        user_agent = self.user_agent

        if self.stealth:
            user_agent.user_agent = user_agent.user_agent.replace("Headless", "")
            user_agent.app_version = user_agent.app_version.replace("Headless", "")

        target_type = ev.target_info.type_
        msg = f"{target_type} <{ev.target_info.url}>"

        domains_patched = []

        if can_use_domain(target_type, "Network"):
            await send_cdp(
                connection,
                "Network.setUserAgentOverride", 
                user_agent.to_json(), 
                ev.session_id, 
            )
            domains_patched.append("Network")
        if can_use_domain(target_type, "Emulation"):
            await send_cdp(
                connection,
                "Emulation.setUserAgentOverride",
                user_agent.to_json(),
                ev.session_id,
            )
            domains_patched.append("Emulation")
        if can_use_domain(target_type, "Runtime"):
            js = load_js("patch_user_agent.js")
            uaPatch = f"const uaPatch = {user_agent.to_json(True, True)};"
            await send_cdp(connection,
                "Runtime.evaluate",
                {
                    "expression": js.replace("//uaPatch//", uaPatch),
                    "includeCommandLineAPI": True,
                },
                ev.session_id)
            domains_patched.append("Runtime")

        if len(domains_patched) == 0:
            logger.info("no domains available to patch user agent for %s", msg)
        else:
            logger.info("successfully patched user agent for %s with domains %s", msg, domains_patched)

    async def handle(self, connection: nodriver.Connection, ev: cdp.target.AttachedToTarget):
        await self.patch_user_agent(connection, ev)