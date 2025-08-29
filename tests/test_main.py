import asyncio
import pytest

from nodriverplus import (
    CrawlResult, 
    CrawlResultHandler,
    NodriverPlus, 
    NodriverPlusManager, 
    ScrapeResponseHandler, 
    ScrapeResponse, 
)
from nodriverplus.utils import to_markdown, get_type


@pytest.mark.suite
@pytest.mark.browser
@pytest.mark.network
@pytest.mark.manager
@pytest.mark.crawl
@pytest.mark.scrape
@pytest.mark.bytes_
@pytest.mark.stealth
@pytest.mark.markdown
@pytest.mark.asyncio
async def test_crawl_with_bytes_and_stealth_and_manager():
    has_html = False
    is_bytes = False
    bytes_conversion_successful = False
    manager_success = False

    # kick up a headless nodriver instance
    # to test stealth
    ndp = NodriverPlus()
    await ndp.start(headless=True)

    manager = NodriverPlusManager(ndp)
    manager.start()

    def handle_html(response: ScrapeResponse):
        nonlocal has_html
        # check if it generated HTML
        if response.html:
            has_html = True

    def handle_bytes(response: ScrapeResponse):
        nonlocal is_bytes, bytes_conversion_successful
        bytes_type = get_type(response.bytes_)
        print("bytes_type: ", bytes_type.__dict__)
        # check if it scraped the bytes/pdf
        if not bytes_type.supported:
            return
        is_bytes = True
        # check if supported bytes to markdown works
        markdown = to_markdown(response.bytes_)
        bytes_conversion_successful = isinstance(markdown, str)

    # attach the handlers to a new ScrapeResponseHandler
    scrape_response_handler = ScrapeResponseHandler(html=handle_html, bytes_=handle_bytes)

    def handle_result(_: CrawlResult):
        nonlocal manager_success
        manager_success = True

    crawl_result_handler = CrawlResultHandler(handle=handle_result)
    await manager.enqueue_crawl(
        "https://investors.lockheedmartin.com/static-files/b5548c6b-71f9-4b58-b171-20accb1e8713",
        scrape_response_handler,
        crawl_result_handler=crawl_result_handler,
        new_window=True
    )
    await manager.wait_for_queue(60)
    # stop the nodriver instance
    await manager.stop()
    await ndp.stop()

    assert has_html
    assert is_bytes
    assert bytes_conversion_successful
    assert manager_success