import asyncio
import pytest

from nodriverplus import (
    CrawlResultHandler,
    NodriverPlus, 
    ScrapeResponseHandler, 
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
    mime_is_pdf = False
    is_bytes = False
    bytes_conversion_successful = False
    manager_success = False

    # kick up a headless nodriver instance
    # to test stealth
    ndp = NodriverPlus()
    await ndp.start(headless=True)

    # use NodriverPlus internal manager helpers

    class ScrapeHandler(ScrapeResponseHandler):
        async def html(self, response):
            nonlocal has_html, mime_is_pdf
            # check if it generated HTML
            if response.html:
                has_html = True
            # check if it generated a PDF
            if response.mime == "application/pdf":
                mime_is_pdf = True

        async def bytes_(self, response):
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

    class CrawlHandler(CrawlResultHandler):
        async def handle(self, result):
            nonlocal manager_success
            manager_success = True

    await ndp.enqueue_crawl(
        "https://investors.lockheedmartin.com/static-files/b5548c6b-71f9-4b58-b171-20accb1e8713",
        ScrapeHandler(),
        crawl_result_handler=CrawlHandler(),
        new_window=True
    )
    await ndp.wait_for_queue(60)
    await ndp.stop()

    assert has_html
    assert mime_is_pdf
    assert is_bytes
    assert bytes_conversion_successful
    assert manager_success