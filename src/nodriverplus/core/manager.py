import asyncio
import logging
import threading
import queue as _thread_queue
from typing import Callable

from .nodriverplus import NodriverPlus
from .scrape_response import ScrapeResponseHandler, CrawlResultHandler

logger = logging.getLogger(__name__)


class ManagerJob:
    url: str
    type_: str
    handler: ScrapeResponseHandler | CrawlResultHandler | None
    kwargs: dict

    def __init__(self, url: str, type_: str, handler: ScrapeResponseHandler | CrawlResultHandler | None, kwargs: dict):
        """lightweight container describing a pending unit of work.

        jobs sit on a thread-safe queue so producer code (maybe running in a different
        thread) can schedule crawls / scrapes without touching the event loop directly.

        :param url: target url for the operation.
        :param type_: either "crawl" or "scrape" (dispatcher uses this).
        :param handler: optional handler instance whose .handle() will receive the result.
        :param kwargs: params forwarded to NodriverPlus.crawl/scrape.
        """
        self.url = url
        self.type_ = type_
        self.handler = handler
        self.kwargs = kwargs

    def from_dict(cls, kwargs: dict):
        """alternate constructor kept for symmetry with export/import paths.

        note: signature mirrors the shape returned by ManagerJob.__dict__ so persisted
        queue snapshots can be rehydrated.
        """
        return cls(
            url=kwargs["url"],
            type_=kwargs["type_"],
            handler=kwargs["handler"],
            kwargs=kwargs["kwargs"]
        )

class NodriverPlusManager:
    queue: _thread_queue.Queue
    ndp: NodriverPlus
    concurrency: int
    _running: bool
    _running_tasks: list[asyncio.Task]
    _thread: threading.Thread | None
    _stop_event: threading.Event
    _done_event: threading.Event
    _runner_task: asyncio.Task | None
    _bound_task: asyncio.Task | None
    _stop_handler: Callable[[list[dict]], any] | None

    def __init__(self, ndp: NodriverPlus, concurrency: int = 1):
        """orchestrates queued scrape/crawl jobs with bounded concurrency.

        a single background coroutine drains a thread-safe queue (put from any thread)
        and spins up asyncio tasks (limited by a semaphore) that call into the shared
        NodriverPlus instance.

        :param ndp: active NodriverPlus instance (already started).
        :param concurrency: max number of simultaneous jobs.
        """
        # use a thread-safe queue for cross-thread communication
        self.queue = _thread_queue.Queue()
        self.ndp = ndp
        self.concurrency = concurrency
        self._thread = None
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._runner_task = None
        self._bound_task = None
        self._stop_handler = None

    async def enqueue_crawl(
        self,
        url: str,
        scrape_response_handler: ScrapeResponseHandler = None,
        depth: int = 1,
        crawl_result_handler: CrawlResultHandler = None,
        *,
        new_window: bool = False,
        scrape_bytes: bool = True,
        navigation_timeout: int = 30,
        wait_for_page_load: bool = True,
        page_load_timeout: int = 60,
        extra_wait_ms: int = 0,
        concurrency: int = 1,
        max_pages: int | None = None,
        collect_responses: bool = False,
        delay_range: tuple[float, float] | None = None,
        tab_close_timeout: float = 5.0,
    ):
        """async wrapper that just enqueues a crawl up to `depth`.

        kept async so user code calling from coroutines reads naturally even though
        the body only uses the thread-safe queue.

        :param crawl_result_handler: optional handler for when the enqueued crawl finishes.
        """
        # enqueue a crawl job (thread-safe queue)
        self.queue.put(ManagerJob(
            url=url,
            type_="crawl",
            handler=crawl_result_handler,
            kwargs={
                "url": url,
                "handler": scrape_response_handler,
                "depth": depth,
                "new_window": new_window,
                "scrape_bytes": scrape_bytes,
                "navigation_timeout": navigation_timeout,
                "wait_for_page_load": wait_for_page_load,
                "page_load_timeout": page_load_timeout,
                "extra_wait_ms": extra_wait_ms,
                "concurrency": concurrency,
                "max_pages": max_pages,
                "collect_responses": collect_responses,
                "delay_range": delay_range,
                "tab_close_timeout": tab_close_timeout,
            }
        ))
        
    async def enqueue_scrape(self,
        url: str,
        scrape_bytes = True,
        scrape_response_handler: ScrapeResponseHandler = None,
        *,
        navigation_timeout = 30,
        wait_for_page_load = True,
        page_load_timeout = 60,
        extra_wait_ms = 0,
        # solve_cloudflare = True, # not implemented yet
        new_tab = False,
        new_window = False,
    ):
        """async wrapper that still just enqueues a single-page scrape.

        kept async so user code calling from coroutines reads naturally even though
        the body only uses the thread-safe queue.

        :param scrape_response_handler: optional handler for when the enqueued scrape finishes.
        """
        self.queue.put(ManagerJob(
            url=url,
            type_="scrape",
            handler=scrape_response_handler,
            kwargs={
                "url": url,
                "scrape_bytes": scrape_bytes,
                "navigation_timeout": navigation_timeout,
                "wait_for_page_load": wait_for_page_load,
                "page_load_timeout": page_load_timeout,
                "extra_wait_ms": extra_wait_ms,
                "new_tab": new_tab,
                "new_window": new_window,
            }
        ))

    def export_queue(self):
        """snapshot (non-destructive) of currently queued ManagerJob objects.

        :return: a python list of `ManagerJob` objects so callers can persist unfinished work on shutdown.
        """
        q = self.queue
        with q.mutex:
            jobs: list[ManagerJob] = list(q.queue)

        logger.debug("exported %d queued job(s)", len(jobs))
        # return a list carrying the jobs
        return jobs

    def import_queue(self, queue: list[ManagerJob]):
        """rehydrate previously exported jobs.

        :param queue: list of jobs to rehydrate
        """
        count = 0
        for j in queue:
            self.queue.put(j)
            count += 1

        logger.info("imported %d queued job(s)", count)

    def start(self, queue: list[dict] | None = None, *, stop_handler: Callable[[list[dict]], any] | None = None):
        """begin draining the job queue on the current event loop.

        safe to call multiple times; subsequent calls while running are ignored.
        optional `stop_handler` receives exported remaining jobs during stop().

        :param queue: optional list of jobs to import before starting
        :param stop_handler: optional callback to receive exported jobs during stop()
        """
        # schedule the run loop as a task on the current asyncio event loop
        if self._runner_task and not self._runner_task.done():
            logger.warning("manager already running")
            return

        # optionally import queued jobs provided as a dict before starting
        if queue:
            self.import_queue(queue)

        # clear any previous stop/done state
        self._stop_event.clear()
        self._done_event.clear()
        # set optional stop handler
        self._stop_handler = stop_handler

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("NodriverPlusManager.start() must be called from within an asyncio event loop")

        self._runner_task = loop.create_task(self._run_loop())

    async def stop(self, timeout: float | None = None):
        """signal the run loop to finish and wait for in-flight tasks.

        any still-queued jobs are exported and passed to the stop handler (if set)
        and also returned to the caller for persistence.

        :param timeout: optional timeout for stopping the manager
        :return: list of exported jobs
        """
        # export queued items before signaling stop so pending jobs are preserved
        exported = self.export_queue()

        # signal the run loop to stop and wake the queue
        self._stop_event.set()
        try:
            # put a sentinel to wake any blocking get()
            self.queue.put(None)
        except Exception:
            logger.exception("failed to enqueue stop sentinel")

        # await the runner task if present
        if self._runner_task:
            try:
                if timeout is not None:
                    await asyncio.wait_for(self._runner_task, timeout)
                else:
                    await self._runner_task
            except asyncio.TimeoutError:
                logger.warning("timeout waiting for manager to stop. cancelling task")
                self._runner_task.cancel()
                try:
                    await self._runner_task
                except Exception:
                    pass
            finally:
                self._runner_task = None
                self._bound_task = None

        # invoke optional stop handler with exported queue
        if self._stop_handler is not None:
            try:
                result = self._stop_handler(exported)
                if asyncio.iscoroutine(result):
                    await result  # type: ignore[arg-type]
            except Exception:
                logger.exception("stop handler failed")
            finally:
                self._stop_handler = None

        return exported

    async def _run_loop(self):
        """internal loop: pulls jobs off the thread-safe queue and runs them.

        concurrency enforced via a semaphore. uses run_in_executor to blockingly
        read from the queue without blocking the event loop.
        """
        sem = asyncio.Semaphore(self.concurrency)
        running_tasks: set[asyncio.Task] = set()

        async def _handle_job(job: ManagerJob):
            msg = f"{job.type_} job <{job.url}>"
            try:
                if job.type_ == "crawl":
                    result = await self.ndp.crawl(**job.kwargs)
                elif job.type_ == "scrape":
                    result = await self.ndp.scrape(**job.kwargs)
                try:
                    if job.handler is not None:
                        if asyncio.iscoroutinefunction(job.handler.handle):
                            await job.handler.handle(result)
                        else:
                            job.handler.handle(result)
                except Exception:
                    logger.exception("error running handler for %s", msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("error running %s", msg)
            finally:
                sem.release()
                # mark this queue item as completed so queue.join() can proceed
                try:
                    self.queue.task_done()
                except Exception:
                    logger.exception("failed to mark %s as done", msg)

        loop = asyncio.get_running_loop()

        while not self._stop_event.is_set():
            # block on the thread-safe queue using a threadpool worker
            job = await loop.run_in_executor(None, self.queue.get)
            # sentinel to stop
            if job is None:
                # account for the sentinel put() to keep unfinished_tasks balanced
                try:
                    self.queue.task_done()
                except Exception:
                    logger.exception("failed to mark stop sentinel as done")
                break

            # respect concurrency of simultaneous jobs
            await sem.acquire()
            task = asyncio.create_task(_handle_job(job))
            running_tasks.add(task)

            def _on_done(t: asyncio.Task):
                running_tasks.discard(t)

            task.add_done_callback(_on_done)

        # wait for any running jobs to finish
        if running_tasks:
            await asyncio.wait(running_tasks)

    async def wait_for_queue(self, timeout: float | None = None):
        """await completion of all queued jobs (queue.join()) with optional timeout.

        :param timeout: optional timeout for waiting on the queue
        """
        # queue.join() is a blocking call; offload to a thread for async compatibility
        if timeout is not None:
            await asyncio.wait_for(asyncio.to_thread(self.queue.join), timeout)
        else:
            await asyncio.to_thread(self.queue.join)
    
