import logging
import nodriver
from nodriver import cdp
from ..target_interceptors import TargetInterceptor
from ...connection import send_cdp
from ...user_agent import UserAgent
from ...cdp_helpers import can_use_domain
from ....js.load import load_text as load_js

logger = logging.getLogger(__name__)

async def patch_user_agent( 
    connection: nodriver.Tab | nodriver.Connection,
    ev: cdp.target.AttachedToTarget | None,
    user_agent: UserAgent,
    stealth: bool = False
):
    """apply UA overrides across relevant domains for a target.

    removes "Headless" when `stealth=True`

    sets Network + Emulation overrides and installs a runtime 
    patch so navigator + related surfaces align. worker/page aware.

    :param connection: `Tab` or `Connection` to apply the patch to.
    :param ev: if `None`, `connection` must be of type `Tab`.
    :param user_agent: prepared UserAgent instance.
    :param stealth: whether to strip "Headless" from user_agent.
    """

    if stealth:
        user_agent.user_agent = user_agent.user_agent.replace("Headless", "")
        user_agent.app_version = user_agent.app_version.replace("Headless", "")

    if isinstance(connection, nodriver.Tab) and ev is None:
        target_type = "tab"
        msg = f"{target_type} <{connection.url}>"
        session_id = None
    else:
        target_type = ev.target_info.type_
        msg = f"{target_type} <{ev.target_info.url}>"
        session_id = ev.session_id

    domains_patched = []

    if can_use_domain(target_type, "Network"):
        await send_cdp(
            connection,
            "Network.setUserAgentOverride", 
            user_agent.to_json(), 
            session_id, 
        )
        domains_patched.append("Network")
    if can_use_domain(target_type, "Emulation"):
        await send_cdp(
            connection,
            "Emulation.setUserAgentOverride",
            user_agent.to_json(),
            session_id,
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
            session_id
        )
        domains_patched.append("Runtime")

    if len(domains_patched) == 0:
        logger.info("no domains available to patch user agent for %s", msg)
    else:
        logger.info("successfully patched user agent for %s with domains %s", msg, domains_patched)

class UserAgentInterceptor(TargetInterceptor):
    """
    stock `TargetInterceptor` for patching user agents
    """
    user_agent: UserAgent
    stealth: bool

    def __init__(self, user_agent: UserAgent, stealth: bool = False):
        self.user_agent = user_agent
        self.stealth = stealth

    async def handle(self, connection: nodriver.Connection, ev: cdp.target.AttachedToTarget):
        await patch_user_agent(connection, ev, self.user_agent, self.stealth)