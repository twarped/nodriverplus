import asyncio
import logging
from nodriverplus import (
    NodriverPlus,
    ScrapeResultHandler,
    CrawlResultHandler,
)

START_URL = "https://example.com"

class HtmlPreviewHandler(ScrapeResultHandler):
    async def handle(self, response):
        print(response.html[:120])

class ResultPrinter(CrawlResultHandler):
    async def handle(self, result):
        print(f"successfully crawled {len(result.successful_links)} page(s)")

async def main():
    logging.basicConfig(level=logging.INFO)
    ndp = NodriverPlus()
    await ndp.start()

    html_handler = HtmlPreviewHandler()
    result_handler = ResultPrinter()

    # enqueue two crawls and a scrape
    ndp.enqueue_crawl(START_URL, scrape_result_handler=html_handler, crawl_result_handler=result_handler)
    ndp.enqueue_crawl(START_URL, scrape_result_handler=html_handler, crawl_result_handler=result_handler)
    ndp.enqueue_scrape(START_URL, scrape_result_handler=html_handler)

    # wait for queue to drain
    await ndp.wait_for_queue()
    # stop manager and collect unfinished jobs (should be empty)
    leftover = await ndp.stop_manager()
    if leftover:
        print(f"unfinished {len(leftover)} jobs persisted")

    # ndp.stop() also stops the manager, but you get the idea
    await ndp.stop()

if __name__ == "__main__":
    asyncio.run(main())
