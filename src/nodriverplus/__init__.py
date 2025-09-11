from .core.nodriverplus import NodriverPlus
from .core.manager import Manager
from .core.user_agent import UserAgent
from .core.scrape_response import (
    CrawlResult, 
    CrawlResultHandler, 
    ScrapeResponse, 
    ScrapeResponseHandler, 
    ScrapeResponseIntercepted, 
    ScrapeRequestIntercepted
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
    "UserAgent",
    "ScrapeResponse",
    "ScrapeResponseHandler",
    "ScrapeResponseIntercepted",
    "ScrapeRequestIntercepted",
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