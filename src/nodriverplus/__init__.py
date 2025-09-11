from .core.nodriverplus import NodriverPlus
from .core.manager import Manager, ManagerJob
from .core.user_agent import UserAgent
from .core.scrape_result import (
    CrawlResult, 
    CrawlResultHandler, 
    ScrapeResult, 
    ScrapeResultHandler, 
    InterceptedResponseMeta, 
    InterceptedRequestMeta
)
from .core.handlers import (
    NetworkWatcher,
    TargetInterceptor,
    TargetInterceptorManager,
    UserAgentPatch,
    WindowSizePatch,
    CloudflareSolver,
)
from . import utils
import nodriver
from nodriver import cdp
from .core.browser import (
    get,
    get_with_timeout,
    stop,
)
from .core.tab import (
    wait_for_page_load,
    get_user_agent,
    crawl,
    scrape,
    click_template_image,
)
from .core.cdp_helpers import (
    TARGET_DOMAINS,
    assert_domain,
    can_use_domain,
    domains_for,
    target_types_for
)

__all__ = [
    "CrawlResult",
    "CrawlResultHandler",
    "nodriver",
    "cdp",
    "NodriverPlus",
    "Manager",
    "ManagerJob",
    "UserAgent",
    "ScrapeResult",
    "ScrapeResultHandler",
    "InterceptedResponseMeta",
    "InterceptedRequestMeta",
    "NetworkWatcher",
    "TargetInterceptor",
    "TargetInterceptorManager",
    "UserAgentPatch",
    "WindowSizePatch",
    "CloudflareSolver",
    "utils",
    "get",
    "get_with_timeout",
    "stop",
    "wait_for_page_load",
    "get_user_agent",
    "crawl",
    "scrape",
    "click_template_image",
    "TARGET_DOMAINS",
    "assert_domain",
    "can_use_domain",
    "domains_for",
    "target_types_for"
]