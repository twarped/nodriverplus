import asyncio
import websockets
import logging
import traceback
from typing import Callable, Awaitable
from nodriver import cdp, Tab, Connection, Browser
from ..cdp_helpers import can_use_domain
import os

logger = logging.getLogger("nodriverplus.TargetInterceptorManager")

class NetworkWatcher:
    """base class for a network watcher managed by `TargetInterceptorManager`

    override methods to handle different CDP network events.

    **NOTE**: this class is tied closely to `TargetInterceptorManager` and
    is not useful on its own.
    
    **TODO**:
    - add `on_loading_finished`?
    - add `on_data_received`?
    - make it less convoluted probably.
    """

    async def on_response(self, 
        tab: Tab,
        ev: cdp.network.ResponseReceived,
        extra_info: cdp.network.ResponseReceivedExtraInfo | None,
    ):
        """
        handle a `ResponseReceived` event
        
        :param connection: the `Tab` the event was received on.
        :param ev: the `ResponseReceived` event.
        :param extra_info: the `ResponseReceivedExtraInfo` event, if available.
        """
        pass

    async def on_response_extra_info(self,
        ev: cdp.network.ResponseReceivedExtraInfo,
    ):
        """
        handle a `ResponseReceivedExtraInfo` event
        
        :param ev: the `ResponseReceivedExtraInfo` event.
        """
        pass

    async def on_loading_failed(self,
        tab: Tab,
        ev: cdp.network.LoadingFailed,
        request_will_be_sent: cdp.network.RequestWillBeSent,
    ):
        """
        handle a `LoadingFailed` event

        :param tab: the `Tab` the event was received on.
        :param ev: the `LoadingFailed` event.
        """
        pass

    async def on_request(self,
        tab: Tab,
        ev: cdp.network.RequestWillBeSent,
        extra_info: cdp.network.RequestWillBeSentExtraInfo | None,
    ):
        """
        handle a `RequestWillBeSent` event

        :param connection: the `Tab` the event was received on.
        :param ev: the `RequestWillBeSent` event.
        :param extra_info: the `RequestWillBeSentExtraInfo` event, if available.
        """
        pass

    async def on_request_extra_info(self,
        ev: cdp.network.RequestWillBeSentExtraInfo
    ):
        """
        handle a `RequestWillBeSentExtraInfo` event

        :param ev: the RequestWillBeSentExtraInfo event.
        """
        pass

    async def stop(self):
        """hook for stopping/cleaning up the watcher if needed"""
        pass


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
        tab: Tab | Connection,
        ev: cdp.target.TargetInfoChanged | None,
    ):
        """hook for handling target change events

        :param tab: the Tab instance where the event was received.
        :param ev: the target info changed event
        :type ev: TargetInfoChanged | None
        """
        pass


class TargetInterceptorManager:

    def __init__(self, 
        session: Tab | Connection | Browser = None, 
        interceptors: list[TargetInterceptor] = None,
        network_watchers: list[NetworkWatcher] = None
    ):
        """init TargetInterceptorManager

        for now, each `TargetInterceptorManager` can only have one session

        :param session: the session that you want to add `TargetInterceptor`'s to.
        :param interceptors: a list of `TargetInterceptor`'s to add to the manager.
        :param network_watchers: a list of `NetworkWatcher`'s to add to the manager.
        """
        self.connection = session.connection if isinstance(session, Browser) else session
        self.interceptors = interceptors or []
        self.network_watchers = network_watchers or []
        self.request_ids_to_tab: dict[str, Tab] = {}
        self.request_sent_events: dict[str, cdp.network.RequestWillBeSent] = {}
        self.response_received_events: dict[str, cdp.network.ResponseReceived] = {}
        self.responses_extra_info: dict[str, cdp.network.ResponseReceivedExtraInfo] = {}
        self.requests_extra_info: dict[str, cdp.network.RequestWillBeSentExtraInfo] = {}
        self.target_id_to_connection: dict[str, Tab | Connection] = {}
        # lifecycle control
        self._stopped = False
        # mapping of CDP event types to manager handler callables
        # allows central dispatch and simpler customization
        self.handler_mappings: dict[type, Callable[[object], Awaitable[None]]] = {
            cdp.target.AttachedToTarget: self.on_attach,
            cdp.target.TargetInfoChanged: self.on_change_interceptors,
            cdp.network.LoadingFailed: self.on_loading_failed,
            cdp.network.RequestWillBeSent: self.on_request_will_be_sent,
            cdp.network.RequestWillBeSentExtraInfo: self.on_request_will_be_sent_extra_info,
            cdp.network.ResponseReceived: self.on_response_received,
            cdp.network.ResponseReceivedExtraInfo: self.on_response_received_extra_info,
        }


    async def _dispatch_event(self, ev: object):
        """central dispatcher that looks up the handler in `self.handler_mappings`
        and calls it with consistent exception handling.

        :param ev: the event instance
        """
        event_type = type(ev)
        if self._stopped:
            return
        handler = self.handler_mappings.get(event_type)
        if handler is None:
            logger.debug("no handler mapped for %s", event_type)
            return
        msg = f"failed to run {handler.__name__} for {event_type}"
        if hasattr(ev, "target_info"):
            msg += f" on {ev.target_info.type_} <{ev.target_info.url}>:"
        else:
            msg += ":"
        def _log_exc_debug(msg, *a):
            logger.debug(msg, *a, exc_info=True)
        def _log_exc_warning(msg, *a):
            logger.warning(msg, *a, exc_info=logger.getEffectiveLevel() <= logging.DEBUG)
        try:
            try:
                await handler(ev)
            except Exception as e:
                current_file = os.path.normcase(os.path.normpath(__file__))
                extracted = traceback.extract_tb(e.__traceback__)
                for frame in extracted:
                    frame_file = os.path.normcase(os.path.normpath(frame.filename))
                    if frame_file != current_file:
                        msg += f"\n{frame.filename}:{frame.lineno} in {frame.name}:\n"
                        break
                raise e
        except (
            websockets.exceptions.ConnectionClosedOK, 
            websockets.exceptions.ConnectionClosedError
        ) as e:
            _log_exc_debug("%s target already moved/closed.", msg)
        except websockets.exceptions.InvalidStatus as e:
            if e.response.body.startswith(b'No such target id:'):
                _log_exc_debug("%s target already moved/closed.", msg)
            else:
                logger.exception(msg)
        except (EOFError, websockets.exceptions.InvalidMessage):
            _log_exc_debug("%s websocket handshake/parse already closed.", msg)
        except Exception as e:
            if "-32000" in str(e):
                _log_exc_warning("%s execution context not created yet.", msg)
            elif "-32001" in str(e):
                _log_exc_warning("%s session not found. (potential timing issue?)", msg)
            elif "-32601" in str(e):
                _log_exc_debug("%s method not found.", msg)
            else:
                logger.exception(msg)


    async def set_hook(
        self,
        ev: cdp.target.AttachedToTarget | None
    ):
        """enable Target.setAutoAttach recursively and attach target interceptors

        attaches recursively to workers/frames and ensures target interceptor application

        :param ev: optional original attach event (when recursively called).
        """
        connection = self.connection
        filters = [
            {"type": "tab", "exclude": True},
            {"type": "iframe", "exclude": True} # can't figure this one out yet
        ]

        if ev:
            msg = f"failed to enable network for {ev.target_info.type_} <{ev.target_info.url}>:"
            session_id = ev.session_id
            # service workers will show up twice if allowed to populate on Page events
            if ev.target_info.type_ == "page":
                filters.append({"type": "service_worker", "exclude": True})
                # network can only be enabled on page targets
                try:
                    await connection.send(cdp.network.enable(), session_id)
                except Exception as e:
                    if "-32001" in str(e):
                        logger.warning("%s session not found. (potential timing issue?)", msg)
                    else:
                        logger.exception(msg)
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
        except Exception as e:
            msg = f"failed to set auto attach for {msg}:"
            if "-32001" in str(e):
                logger.warning("%s session not found. (potential timing issue?)", msg)
            elif "-32601" in str(e):
                logger.warning("%s method not found", msg)
            else:
                logger.exception(msg)


    # `target_id` and `frame_id` are the same for tabs
    # so as long as we only enable network for page targets
    # we can find the tab like this: 
    # (if `None`, it probably doesn't matter)
    async def on_response_received(self, ev: cdp.network.ResponseReceived):
        """CDP handler fired when a network response is received.

        passes the event to all `NetworkWatcher.on_response` handlers.
        """
        if self._stopped:
            return
        tab = next((t for t in self.connection.browser.tabs if t.target_id == ev.frame_id), None)
        if tab is None:
            logger.debug("no tab found for ResponseReceived <%s> with target_id %s", ev.response.url, ev.frame_id)
            return
        for handler in self.network_watchers:
            await handler.on_response(tab, ev, self.responses_extra_info.get(ev.request_id))
        self.responses_extra_info.pop(ev.request_id, None)


    async def on_loading_failed(self, ev: cdp.network.LoadingFailed):
        """CDP handler fired when a network request fails to load.

        passes the event to all `NetworkWatcher.on_loading_failed` handlers.
        """
        if self._stopped:
            return
        tab = self.request_ids_to_tab.get(ev.request_id)
        if tab is None:
            logger.debug("no tab found for LoadingFailed with request_id %s", ev.request_id)
            return

        for handler in self.network_watchers:
            await handler.on_loading_failed(tab, ev, self.request_sent_events.get(ev.request_id))
        self.request_ids_to_tab.pop(ev.request_id, None)


    async def on_response_received_extra_info(self, ev: cdp.network.ResponseReceivedExtraInfo):
        """CDP handler fired when extra information about a network response is received.

        passes the event to all `NetworkWatcher.on_response_extra_info` handlers.
        """
        if self._stopped:
            return
        self.responses_extra_info[ev.request_id] = ev
        for handler in self.network_watchers:
            await handler.on_response_extra_info(ev)


    async def on_request_will_be_sent(self, ev: cdp.network.RequestWillBeSent):
        """CDP handler fired when a network request is about to be sent.

        passes the event to all `NetworkWatcher.on_request` handlers.
        """
        if self._stopped:
            return
        tab = next((t for t in self.connection.browser.tabs if t.target_id == ev.frame_id), None)
        if tab is None:
            logger.debug("no tab found for RequestWillBeSent <%s> with target_id %s", ev.request.url, ev.frame_id)
            return
        self.request_ids_to_tab[ev.request_id] = tab
        # store the request event so LoadingFailed handlers can access original url
        self.request_sent_events[ev.request_id] = ev
        for handler in self.network_watchers:
            await handler.on_request(tab, ev, self.requests_extra_info.get(ev.request_id))
        self.requests_extra_info.pop(ev.request_id, None)

    
    async def on_request_will_be_sent_extra_info(self, ev: cdp.network.RequestWillBeSentExtraInfo):
        """CDP handler fired when extra information about a network request is received.

        passes the event to all `NetworkWatcher.on_request_extra_info` handlers.
        """
        if self._stopped:
            return
        self.requests_extra_info[ev.request_id] = ev
        for handler in self.network_watchers:
            await handler.on_request_extra_info(ev)


    async def on_change_interceptors(
        self,
        ev: cdp.target.TargetInfoChanged,
    ):
        """execute a list of `TargetInterceptor.on_change` calls—(in order)—to
        `self.connection` with the `TargetInfoChanged` event.

        :param ev: the event to pass to the interceptors.
        """
        if self._stopped:
            return
        target_msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
        connection = self.target_id_to_connection.get(ev.target_info.target_id) or self.connection
        for interceptor in self.interceptors:
            ev.session_id = None
            logger.debug("calling interceptor (on_change) %s to %s", interceptor, target_msg)
            await interceptor.on_change(connection, ev)


    async def on_attach_interceptors(
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
        if self._stopped:
            return
        for interceptor in self.interceptors:
            if ev:
                msg = f"{interceptor} to {target_msg}"
            else:
                msg = f"{interceptor} to {self.connection}"
            logger.debug("calling interceptor (on_attach) %s", msg)
            await interceptor.on_attach(self.connection, ev)


    async def on_attach(
        self,
        ev: cdp.target.AttachedToTarget | None,
    ):        
        """handler fired when a new target is auto-attached.

        applies `TargetInterceptor`s and recursively attaches to child sessions/targets

        :param ev: CDP AttachedToTarget event.
        """
        if self._stopped:
            return
        connection = self.connection

        if ev is not None:
            msg = f"{ev.target_info.type_} <{ev.target_info.url}>"
            session_id = ev.session_id
            tab = next((t for t in connection.browser.tabs if t.target_id == ev.target_info.target_id), None)
            if tab:
                self.target_id_to_connection[ev.target_info.target_id] = tab
            else:
                self.target_id_to_connection[ev.target_info.target_id] = connection
        else:
            msg = connection
            session_id = None
        logger.debug("successfully attached to %s", msg)

        # call on_attach interceptors
        await self.on_attach_interceptors(ev)
        # recursive attachment and TargetInfoChanged handling
        await self.set_hook(ev)
        # continue like normal
        try:
            logger.debug("resuming %s (session_id=%s)", msg, session_id)
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

        # register all mapped handlers to route through the central dispatcher
        for ev_type in self.handler_mappings.keys():
            connection.add_handler(ev_type, self._dispatch_event)
        await self.set_hook(None)

    async def stop(self):
        """stop all background tasks

        remove handlers and stop all created 
        subprocesses/threads if available.
        """
        if self._stopped:
            return
        self._stopped = True
        # remove handlers we previously added to avoid further callback dispatch after websocket closes
        for ev_type in self.handler_mappings.keys():
            self.connection.remove_handler(ev_type, self._dispatch_event)
        
        for watcher in self.network_watchers:
            try:
                # support both sync and async `stop()` implementations
                res = watcher.stop()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.exception("error stopping watcher %s", watcher)