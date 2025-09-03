import logging
from typing import Callable, Coroutine
from nodriver import cdp, Tab, Connection, Browser
import nodriver
from ..cdp_helpers import can_use_domain

logger = logging.getLogger(__name__)

class TargetInterceptor:
    """base class for a target interceptor

    you must provide a `handle()` method that takes a connection and an event.

    called by `apply_target_interceptors()`
    """

    async def handle(
        self,
        connection: Tab | Connection,
        ev: cdp.target.AttachedToTarget | None
    ):
        """hook for handling target attachment events

        :param connection: the connection to the target.
        :param ev: **may be `None`** depending on how you call it, so be aware.
        :type ev: AttachedToTarget | None
        """
        pass
    

class TargetInterceptorManager:
    connection: Tab | Connection
    interceptors: list[TargetInterceptor]

    def __init__(self, 
        session: Tab | Connection | Browser = None, 
        interceptors: list[TargetInterceptor] = []
    ):
        """init TargetInterceptorManager

        for now, each `TargetInterceptorManager` can only have one session
        :param session: the session that you want to add `TargetInterceptor`'s to.
        :param interceptors: a list of `TargetInterceptor`'s to add to the manager.
        """
        self.connection = session.connection if isinstance(session, Browser) else session
        self.interceptors = interceptors


    async def set_hook(
        self,
        ev: cdp.target.AttachedToTarget | None
    ):
        """enable Target.setAutoAttach recursively and attach target interceptors

        attaches recursively to workers/frames and ensures target interceptor application

        :param ev: optional original attach event (when recursively called).
        """
        connection = self.connection
        filters = [{"type": "tab", "exclude": True}]

        if ev:
            msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
            session_id = ev.session_id
            # service workers will show up twice if allowed to populate on Page events
            if ev.target_info.type_ == "page":
                filters.append({"type": "service_worker", "exclude": True})
        else:
            msg = connection
            session_id = None
        filters.append({})
        try:
            await connection.send(cdp.target.set_auto_attach(
                auto_attach=True,
                wait_for_debugger_on_start=True,
                flatten=True,
                filter_=cdp.target.TargetFilter(filters)
            ), session_id)
            logger.info("successfully set auto attach for %s", msg)
        except Exception:
            logger.exception("failed to set auto attach for %s:", msg)



    async def apply_interceptors(
        self,
        ev: cdp.target.AttachedToTarget | None,
    ):
        """apply a list of target interceptors—(in order)—to 
        `self.connection` with the `AttachedToTarget` event if available.

        :param ev: the event to pass to the interceptors.
        """
        if ev:
            msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
        elif isinstance(self.connection, Tab):
            msg = f"tab <{self.connection.url}>"
        else:
            msg = f"connection <{self.connection}>"
        for interceptor in self.interceptors:
            if ev:
                msg = f"{interceptor} to {msg}"
            else:
                msg = f"{interceptor} to {self.connection}"
            try:
                await interceptor.handle(self.connection, ev)
            except Exception as e:
                if "-32000" in str(e):
                    logger.warning("failed to apply interceptor %s: execution context not created yet", msg)
                else: 
                    raise


    async def on_attach(
        self,
        ev: cdp.target.AttachedToTarget | None,
    ):        
        """handler fired when a new target is auto-attached.

        applies `TargetInterceptor`s and recursively attaches to child sessions/targets

        :param ev: CDP AttachedToTarget event.
        """
        connection = self.connection

        if ev is not None:
            msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
            session_id = ev.session_id
        else:
            msg = connection
            session_id = None
        logger.info("successfully attached to %s", msg)

        # apply interceptors
        await self.apply_interceptors(ev)
        # recursive attachment
        await self.set_hook(ev)
        # continue like normal
        try:
            await connection.send(cdp.runtime.run_if_waiting_for_debugger(), session_id)
        except Exception as e:
            if "-32001" in str(e):
                logger.warning("too slow resuming %s", msg)
            else:
                logger.exception("failed to resume %s:", msg)
        else:
            logger.debug("successfully resumed %s", msg)


    async def start(
        self,
    ):
        """
        start the hook on `self.connection` to recursively apply `TargetInterceptor`s.
        """
        connection = self.connection

        # avoid duping stuff
        if getattr(connection, "_target_interceptor_manager_initialized", False):
            return
        setattr(connection, "_target_interceptor_manager_initialized", True)

        connection.add_handler(cdp.target.AttachedToTarget, self.on_attach)
        await self.set_hook(None)