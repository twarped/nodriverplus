import logging
from typing import Callable, Coroutine
from nodriver import cdp, Tab, Connection, Browser
from ..cdp_helpers import TARGET_DOMAINS
from ..connection import send_cdp

logger = logging.getLogger(__name__)

class TargetInterceptor:
    """base class for a target interceptor

    they must provide a handle method that takes a connection and an event.

    called by `apply_target_interceptors()`
    """

    async def handle(
        connection: Tab | Connection,
        ev: cdp.target.AttachedToTarget
    ):
        pass

    def __init__(self, 
        handle: Callable[[Tab | Connection, cdp.target.AttachedToTarget], Coroutine[any, any, None]] | None = None
    ):
        if handle:
            self.handle = handle


async def autohook_connection(
    connection: Tab | Connection, 
    ev: cdp.target.AttachedToTarget = None
):
    """enable Target.setAutoAttach recursively and attach target interceptors

    attaches recursively to workers/frames and ensures target interceptor application

    :param connection: tab/connection/browsers connection.
    :param ev: optional original attach event (when recursively called).
    """
    types = list(TARGET_DOMAINS.keys())
    types.remove("tab")

    # hangs on service workers if not deduped
    if getattr(connection, "_already_attached", False):
        return
    setattr(connection, "_already_attached", True)

    try:
        await send_cdp(connection, "Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": True,
            "flatten": True,
            "filter": [{"type": t, "exclude": False} for t in types]
        }, ev.session_id if ev else None)
    except Exception:
        logger.exception("auto attach failed for %s:", 
    f"{ev.target_info.type_} <{ev.target_info.url}>" if ev else connection)


async def apply_target_interceptors(
    connection: Tab | Browser | Connection,
    ev: cdp.target.AttachedToTarget, 
    target_interceptors: list[TargetInterceptor]
):
    """apply a list of target interceptors--in order--to a connection and event.

    :param connection: the connection to apply the interceptors to.
    :param ev: the event to pass to the interceptors.
    :param target_interceptors: the ordered list of target interceptors to apply.
    """
    for interceptor in target_interceptors:
        await interceptor.handle(connection, ev)
        
        
async def on_attach_target_interceptors(
    session: Tab | Browser | Connection,
    ev: cdp.target.AttachedToTarget,
    target_interceptors: list[TargetInterceptor]
):        
    """handler fired when a new target is auto-attached.

    applies `TargetInterceptor`s and recursively attaches to child sessions/targets

    :param ev: CDP AttachedToTarget event.
    :param session: object providing .connection (browser or tab or connection it.
    """
    connection = session.connection if isinstance(session, Browser) else session

    logger.info("successfully attached to %s", f"{ev.target_info.type_} <{ev.target_info.url}>")
    # apply target_interceptors
    # TODO: turn patch_user_agent into a TargetInterceptor
    await apply_target_interceptors(connection, ev, target_interceptors)

    # recursive attachment
    await autohook_connection(connection, ev)
    # continue like normal
    msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
    try:
        await send_cdp(connection, "Runtime.runIfWaitingForDebugger", session_id=ev.session_id)
    except Exception as e:
        if "-3200" in str(e):
            logger.warning("too slow resuming %s", msg)
        else:
            logger.exception("failed to resume %s:", msg)
    else:
        logger.info("successfully resumed %s", msg)


async def setup_target_interceptor_autoattach(session: Tab | Browser | Connection):
    """one-time setup on a root connection to enable recursive `TargetInterceptor` application.

    :param session: target session for the operation.
    """
    connection = session.connection if isinstance(session, Browser) else session

    # avoid duping stuff
    if getattr(connection, "_target_interceptors_initialized", False):
        return
    setattr(connection, "_target_interceptors_initialized", True)

    connection.add_handler(cdp.target.AttachedToTarget, on_attach_target_interceptors)
    await autohook_connection(connection)