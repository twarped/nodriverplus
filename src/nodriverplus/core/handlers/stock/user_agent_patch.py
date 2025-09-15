import logging
import nodriver
from nodriver import cdp
from ..target_intercepted import TargetInterceptor
from ...user_agent import UserAgent
from ...cdp_helpers import can_use_domain
from ....js.load import load_text as load_js

logger = logging.getLogger("nodriverplus.UserAgentPatch")


class UserAgentPatch(TargetInterceptor):
    """
    stock `TargetInterceptor` for patching the user agent
    across relevant domains for a target.

    call `UserAgentPatch.patch_user_agent()` directly—with
    or without creating a new instance—to patch
    an existing `Tab` or `Connection`.
    """
    user_agent: UserAgent
    hide_headless: bool

    def __init__(self, user_agent: UserAgent, hide_headless: bool = False):
        self.user_agent = user_agent
        self.hide_headless = hide_headless

    async def on_attach(self, connection, ev):
        await self.patch_user_agent(connection, ev, self.user_agent, self.hide_headless)

    async def on_change(self, connection, ev):
        try:
            await self.patch_user_agent(connection, ev, self.user_agent, self.hide_headless)
        except Exception as e:
            if "-32601" in str(e):
                logger.debug("(on_change) domain not available to patch user agent for %s <%s>:\n  %s",
                    ev.target_info.type_, ev.target_info.url, e)

    @staticmethod
    async def patch_user_agent(
        connection: nodriver.Tab | nodriver.Connection,
        ev: cdp.target.AttachedToTarget | None,
        user_agent: UserAgent,
        hide_headless: bool = False
    ):
        """static method for applying UA overrides
        across relevant domains for a target.

        removes "Headless" when `hide_headless=True`

        sets Network + Emulation overrides and installs a runtime 
        patch so navigator + related surfaces align. worker/page aware.

        :param connection: `Tab` or `Connection` to apply the patch to.
        :param ev: if `None`, `connection` must be of type `Tab`.
        :param user_agent: prepared UserAgent instance.
        :param hide_headless: whether to strip "Headless" from user_agent.
        """

        if hide_headless:
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
            logger.debug("patching user agent for %s with domain: \"Network\"", msg)
            await connection.send(cdp.network.set_user_agent_override(
                user_agent=user_agent.user_agent,
                accept_language=user_agent.accept_language,
                platform=user_agent.platform,
                user_agent_metadata=user_agent.metadata,
            ), session_id)
            domains_patched.append("Network")
        if can_use_domain(target_type, "Emulation"):
            logger.debug("patching user agent for %s with domain: \"Emulation\"", msg)
            await connection.send(cdp.emulation.set_user_agent_override(
                user_agent=user_agent.user_agent,
                accept_language=user_agent.accept_language,
                platform=user_agent.platform,
                user_agent_metadata=user_agent.metadata,
            ), session_id)
            domains_patched.append("Emulation")
            
        js = load_js("patch_user_agent.js")
        uaPatch = f"const uaPatch = {user_agent.to_json(True, True)};"
        script = js.replace("//uaPatch//", uaPatch)
        if can_use_domain(target_type, "Page"):
            await connection.send(cdp.page.add_script_to_evaluate_on_new_document(
                source=script,
                include_command_line_api=True,
            ), session_id)
        if can_use_domain(target_type, "Runtime"):
            await connection.send(cdp.runtime.evaluate(
                expression=script,
                include_command_line_api=True,
                allow_unsafe_eval_blocked_by_csp=True
            ), session_id)
            domains_patched.append("Runtime")

        if len(domains_patched) == 0:
            logger.info("no domains available to patch user agent for %s", msg)
        else:
            logger.debug("successfully patched user agent for %s with domains %s", msg, domains_patched)

