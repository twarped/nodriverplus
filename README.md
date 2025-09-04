# nodriverplus
this is a framework for advanced stealth and crawling using [ultrafunkamsterdam's `nodriver`](https://github.com/ultrafunkamsterdam/nodriver)

## **NOTE:**
this library **depends** on [`twarped/nodriver`](https://github.com/twarped/nodriver). if you try using [`ultrafunkamsterdam/nodriver`](https://github.com/ultrafunkamsterdam/nodriver) instead, this library *will not work*.

`ultrafunkamsterdam/nodriver` is architecturally missing these key **Chrome Devtools Protocol (CDP)** features:
- `sessionId` support for sending CDP commands to a non-`Tab` session
  - [feat: included `sessionId` in `tab.send()` transaction](https://github.com/twarped/nodriver/commit/bf1dfda6cb16a31d2fd302f370f130dda3a3413b)
- `browserContextId` support for creating new `Target`'s designated to a certain browser context
  - [feat: added `browser_context_id` support with tab and browser `get`](https://github.com/twarped/nodriver/commit/1dcb52e8063bad359a3f2978b83f44e20dfbca68)
- not really "key" architecture issues here, but definitely nice to haves: 
  - [fix(connection): patched race condition in `_register_handlers()`](https://github.com/twarped/nodriver/commit/fe0d05dcd6180e77350120479a3d073bf86cc9a8)
  - [fix: ignore `InvalidStateError` if transaction is already finished](https://github.com/twarped/nodriver/commit/5fca5b4b22f37af47194b844d6e4d062be777a14)

# **TODO**
this library is mostly just a working proof of concept as of now, and will need some work still to really make it what I want it to be.

- [ ] make timed_out_navigating vs timed_out_loading configurable in `crawl()`
- [ ] fix stealth patch leaks (native masking)
- [ ] ensure that links are deduped when crawling
- [ ] fix `Manager` issue:
	- [ ] doesn't stop on ctrl+c. you have to manually terminate the process
- [ ] add more granular tests
- [ ] solve cloudflare checkbox
- [ ] solve datadome puzzle slider
- [ ] migrate low level functions from `NodriverPlus` into separate files like `tab.py` or something
	- [ ] then attach the high level `NodriverPlus` to those functions to make it more maintainable
- [ ] turn stealth and `scrape_bytes` logic/functionality into target_interceptors/strings of functions that will occur in each of these events:
	- [ ] target creation/attachment
	- [ ] request interception
	- [ ] response interception
	- [ ] make sure that it can still follow redirects and stuff though
	- [ ] and stay perfectly timed and stuff too
- [ ] add target_interceptors: `ScrapeRequestInterceptor` and `ScrapeResponseInterceptor`
- [ ] turn handwritten target_interceptors into ones using the new api
- [ ] add option to receive bytes as stream on `ScrapeResponse` instead of a cached var
- [ ] update `CrawlResultHandler` and `Manager` to handle errors and stuff
- [ ] make `pymupdf` optional
# usage

example if you just want the stealth:
```python
from nodriverplus import NodriverPlus

# hide_headless defaults to on
ndp = NodriverPlus() # `hide_headless=True`
browser = await ndp.start() # headless or headful

# for a graceful shutdown: (takes longer)
await ndp.stop()
# immediate shutdown (leaves an exception)
# or just `browser.stop()` has the same effect
await ndp.stop(graceful=False)
```

example if you want to see more logs than just errors/warnings:
```python
import logging
from nodriverplus import NodriverPlus

# set your log level here:
logging.basicConfig(level=logging.INFO)

ndp = NodriverPlus()
await ndp.start()
await ndp.stop()
```

example if you want to scrape:
```python
from nodriverplus import NodriverPlus

ndp = NodriverPlus()
await ndp.start() # headless or headful

response = await ndp.scrape("https://example.com")
print(response.html[:500])

await ndp.stop()
```

example if you want to crawl:
```python
from nodriverplus import NodriverPlus, ScrapeResponseHandler

ndp = NodriverPlus()
await ndp.start() # headless or headful

class MyCustomHandler(ScrapeResponseHandler):

    async def html(self, response):
        print(response.html[:500])

    async def links(self, response):
        links_to_crawl = []
        for link in response.links:
        links_to_crawl.append(link)
        print(f"found {len(links_to_crawl)} links to crawl")
        return links_to_crawl

result = await ndp.crawl("https://example.com", depth=2, handler=MyCustomHandler())

await ndp.stop()
```

example if you want to run multiple concurrent crawls in the background:

handy if you want to run crawls and stuff from an http server
```python
from nodriverplus import (
    NodriverPlus,
    Manager,
    ScrapeResponseHandler,
    CrawlResultHandler,
)

ndp = NodriverPlus()
await ndp.start() # again, headless or headful

manager = Manager(ndp, concurrency=2)
manager.start()

# simple result handlers
class ScrapeHandler(ScrapeResponseHandler):
    async def html(self, response):
        print(response.html[:500])

class CrawlHandler(CrawlResultHandler):
    async def handle(self, result):
        print(f"successfully crawled {len(result.successful_links)}")

# enqueue a crawl
await manager.enqueue_crawl("https://example.com",
    scrape_response_handler=ScrapeHandler(),
    crawl_result_handler=CrawlHandler()
)

# enqueue another crawl
await manager.enqueue_crawl("https://example.com",
    scrape_response_handler=ScrapeHandler(),
    crawl_result_handler=CrawlHandler()
)

# enqueue a scrape
await manager.enqueue_scrape("https://example.com",
    scrape_response_handler=ScrapeHandler()
)

# optional:
# wait for the queue to finish
await manager.wait_for_queue()

# important:
# stop the manager when you're done
unfinished_queue = await manager.stop()
# optional:
# save the unfinished queue for another time:
save_queue_somehow(unfinished_queue)

await ndp.stop()
```


# usage theories

you could probably execute some complicated crawls using the link handler.

for example, you could set up a pre-picked list of links or link generator to crawl and feed it to the `links` method of the `ScrapeResponseHandler` with `crawl(depth=float('inf'), concurrency=1)` or something

or maybe using `ScrapeResponse.tab` in an async `ScrapeResponseHandler` method to execute javascript and stuff in a tab before continuing the current link tree