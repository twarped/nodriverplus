import asyncio
from nodriverplus import NodriverPlus

URL = "https://example.com"

async def main():
    ndp = NodriverPlus()
    await ndp.start()
    resp = await ndp.scrape(URL)
    # print a preview of the document
    print(resp.html[:500])
    await ndp.stop()

if __name__ == "__main__":
    asyncio.run(main())
