from .target_intercepted import (
    TargetInterceptor,
    TargetInterceptorManager,
    NetworkWatcher,
)
from .stock import (
    UserAgentPatch,
    # ScrapeRequestPausedHandler
    WindowSizePatch,
    CloudflareSolver,
)
from .result import (
    ScrapeResult,
    ScrapeResultHandler,
    CrawlResult,
    CrawlResultHandler,
    InterceptedResponseMeta,
    InterceptedRequestMeta,
)
from .request_paused import RequestPausedHandler

__all__ = [
    "UserAgentPatch",
    "TargetInterceptor",
    "TargetInterceptorManager",
    "NetworkWatcher",
    "WindowSizePatch",
    # "ScrapeRequestPausedHandler",
    "CloudflareSolver",
    "ScrapeResult",
    "ScrapeResultHandler",
    "CrawlResult",
    "CrawlResultHandler",
    "InterceptedResponseMeta",
    "InterceptedRequestMeta",
    "RequestPausedHandler",
]