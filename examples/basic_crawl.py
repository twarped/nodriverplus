import asyncio
from nodriverplus import NodriverPlus, ScrapeResponseHandler

START_URL = "https://example.com"

# simple handler that prints html preview and returns links unchanged
class ExampleHandler(ScrapeResponseHandler):
    async def html(self, response):  # type: ignore[override]
        print(response.html[:500])
    async def links(self, response):  # type: ignore[override]
        # optionally mutate which links to follow
        print(f"found {len(response.links)} links to crawl")
        return response.links

async def main():
    ndp = NodriverPlus()
    await ndp.start()
    handler = ExampleHandler()
    result = await ndp.crawl(START_URL, depth=2, scrape_response_handler=handler)
    print(f"crawl finished pages={len(result.successful_links)} failed={len(result.failed_links)}")
    await ndp.stop()

if __name__ == "__main__":
    asyncio.run(main())
