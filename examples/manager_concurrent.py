import asyncio
import logging
from nodriverplus import (
    NodriverPlus,
    NodriverPlusManager,
    ScrapeResponseHandler,
    CrawlResultHandler,
)

START_URL = "https://example.com"

class HtmlPreviewHandler(ScrapeResponseHandler):
    async def handle(self, response):
        print(response.html[:120])

class ResultPrinter(CrawlResultHandler):
    async def handle(self, result):
        print(f"successfully crawled {len(result.successful_links)} page(s)")

async def main():
    logging.basicConfig(level=logging.INFO)
    ndp = NodriverPlus()
    await ndp.start()

    manager = NodriverPlusManager(ndp, concurrency=2)
    manager.start()

    html_handler = HtmlPreviewHandler()
    result_handler = ResultPrinter()

    # enqueue two crawls and a scrape
    await manager.enqueue_crawl(START_URL, scrape_response_handler=html_handler, crawl_result_handler=result_handler)
    await manager.enqueue_crawl(START_URL, scrape_response_handler=html_handler, crawl_result_handler=result_handler)
    await manager.enqueue_scrape(START_URL, scrape_response_handler=html_handler)

    # wait for queue to drain
    await manager.wait_for_queue()
    # stop manager and collect unfinished jobs (should be empty)
    leftover = await manager.stop()
    if leftover:
        print(f"unfinished {len(leftover)} jobs persisted")

    await ndp.stop()

if __name__ == "__main__":
    asyncio.run(main())
