import asyncio
import pytest

from nodriverplus import NodriverPlus, ScrapeResultHandler

import logging
logging.basicConfig(level=logging.INFO)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.browser,
    pytest.mark.crawl,
    pytest.mark.asyncio,
]

class Duper(ScrapeResultHandler):
    async def links(self, result):
        return ["https://example.com", "https://example.com"]

async def _run_crawl(url: str, depth: int = 4):
    ndp = NodriverPlus()
    await ndp.start(headless=True)
    result = await ndp.crawl(url, scrape_result_handler=Duper(), depth=depth, concurrency=2)
    await ndp.stop()
    return result

async def collect_crawl(url: str = "https://example.com"):
    return await _run_crawl(url)

async def test_crawl_deduplication():
    result = await collect_crawl()

    # ensure no duplicate discovered links
    assert len(result.links) == len(set(result.links)), "duplicate URLs found in result.links"
    # ensure no duplicate successful links
    assert len(result.successful_links) == len(set(result.successful_links)), "duplicate URLs found in successful_links"

    # sanity: any link marked failed/timed out shouldn't also appear in successful twice
    dup_success = [u for u in result.successful_links if result.successful_links.count(u) > 1]
    assert not dup_success, f"duplicates in successful_links: {dup_success}"

asyncio.run(test_crawl_deduplication())