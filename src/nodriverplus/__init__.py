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
from nodriverplus.core.tab import (
    wait_for_page_load,
    get_user_agent,
    crawl,
    scrape,
    click_template_image,
)
from .core.handlers import stock
from . import utils
import nodriver
from nodriver import cdp

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
    "utils",
    "wait_for_page_load",
    "get_user_agent",
    "crawl",
    "scrape",
    "click_template_image",
    "stock",
]