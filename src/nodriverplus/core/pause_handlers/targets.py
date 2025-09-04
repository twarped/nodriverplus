import logging
from typing import Callable, Coroutine
from nodriver import cdp, Tab, Connection, Browser
import nodriver
from ..cdp_helpers import can_use_domain

logger = logging.getLogger(__name__)

class TargetInterceptor:
    """base class for a target interceptor

    you must provide a `on_attach()` method that takes a connection and an event.

    called by `apply_target_interceptors()`
    """

    async def on_attach(
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

    async def on_change(
        self,
        connection: Tab | Connection,
        ev: cdp.target.TargetInfoChanged | None,
        session_id: str,
    ):
        """hook for handling target change events

        :param connection: the connection to the target.
        :param ev: the target info changed event
        :type ev: TargetInfoChanged | None
        """
        pass


class TargetInterceptorManager:
    connection: Tab | Connection
    interceptors: list[TargetInterceptor]
    # [target_id: session_id]
    session_ids: dict[str, str]

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
        self.session_ids = {}


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
            self.session_ids[ev.target_info.target_id] = session_id
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
            logger.debug("successfully set auto attach for %s", msg)
        except Exception:
            logger.exception("failed to set auto attach for %s:", msg)


    async def interceptors_on_change(
        self,
        ev: cdp.target.TargetInfoChanged,
    ):
        """execute a list of `TargetInterceptor.on_change` calls—(in order)—to
        `self.connection` with the `TargetInfoChanged` event.

        :param ev: the event to pass to the interceptors.
        """
        target_msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
        for interceptor in self.interceptors:
            msg = f"{interceptor} to {target_msg}"
            try:
                await interceptor.on_change(self.connection, ev, self.session_ids.get(ev.target_info.target_id))
            except Exception as e:
                if "-32000" in str(e):
                    logger.warning("failed to apply interceptor (on_change) %s: execution context not created yet", msg)
                elif "-32001" in str(e):
                    logger.warning("failed to apply interceptor (on_change) %s: session not found. (potential timing issue?)", msg)
                elif "-32601" in str(e):
                    logger.warning("failed to apply interceptor (on_change) %s: method not found", msg)
                else: 
                    logger.exception("failed to apply interceptor (on_change) %s:", msg)


    async def interceptors_on_attach(
        self,
        ev: cdp.target.AttachedToTarget | None,
    ):
        """execute a list of `TargetInterceptor.on_attach` calls—(in order)—to
        `self.connection` with the `AttachedToTarget` event if available.

        :param ev: the event to pass to the interceptors.
        """
        if ev:
            target_msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
        elif isinstance(self.connection, Tab):
            target_msg = f"tab <{self.connection.url}>"
        else:
            target_msg = f"connection <{self.connection}>"
        for interceptor in self.interceptors:
            if ev:
                msg = f"{interceptor} to {target_msg}"
            else:
                msg = f"{interceptor} to {self.connection}"
            try:
                logger.debug("applying interceptor (on_attach) %s", msg)
                await interceptor.on_attach(self.connection, ev)
            except Exception as e:
                if "-32000" in str(e):
                    logger.warning("failed to apply interceptor (on_attach) %s: execution context not created yet", msg)
                else: 
                    logger.exception("failed to apply interceptor (on_attach) %s:", msg)


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
        logger.debug("successfully attached to %s", msg)

        # apply interceptors
        await self.interceptors_on_attach(ev)
        # recursive attachment and TargetInfoChanged handling
        await self.set_hook(ev)
        # continue like normal
        try:
            logger.debug("resuming target %s (session_id=%s)", msg, session_id)
            await connection.send(cdp.runtime.run_if_waiting_for_debugger(), session_id)
        except Exception as e:
            if "-32001" in str(e):
                logger.warning("session for %s not found. (potential timing issue?)", msg)
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
        # subscribe to target changes as well
        connection.add_handler(cdp.target.TargetInfoChanged, self.interceptors_on_change)
        await self.set_hook(None)