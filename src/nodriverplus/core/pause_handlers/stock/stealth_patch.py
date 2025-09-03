from nodriver import cdp
import nodriver
import logging
from ..targets import TargetInterceptor
from ....js.load import load_text as load_js
from ...cdp_helpers import can_use_domain

logger = logging.getLogger(__name__)

async def patch_stealth( 
    connection: nodriver.Tab | nodriver.Connection, 
    ev: cdp.target.AttachedToTarget | None
):
    """inject stealth patch into a target (runtime + early document script).

    chooses worker/page variant based on target type.

    :param ev: attach event describing the target.
    """
    # handle ev == None for direct tab patching
    if isinstance(connection, nodriver.Tab) and ev is None:
        target_type = "tab"
        session_id = None
        msg = f"tab <{connection.url}>"
    else:
        target_type = ev.target_info.type_
        session_id = ev.session_id
        msg = f"{target_type} <{ev.target_info.url}>"

    # load and apply the stealth patch
    name = "apply_stealth.js"
    if target_type in {"service_worker", "shared_worker"}:
        name = "apply_stealth_worker.js"
    js = load_js(name)
    logger.debug("injecting stealth patch into %s", msg)

    # try adding the patch to the page
    if can_use_domain(target_type, "Page"):
        await connection.send(cdp.page.add_script_to_evaluate_on_new_document(
            source=js,
            include_command_line_api=True,
            run_immediately=True
        ), session_id)
    if can_use_domain(target_type, "Runtime"):
        await connection.send(cdp.runtime.evaluate(
            expression=js,
            include_command_line_api=True,
            await_promise=True,
            allow_unsafe_eval_blocked_by_csp=True
        ), session_id=session_id)

class StealthPatch(TargetInterceptor):
    """stock `TargetInterceptor` for applying stealth patches to targets.

    utilizes `apply_stealth()` to inject js stealth patches into a connection target
    """

    async def handle(self, 
        connection: nodriver.Tab | nodriver.Connection, 
        ev: cdp.target.AttachedToTarget,
    ):
        await patch_stealth(connection, ev)