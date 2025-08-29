import asyncio
import logging
from nodriverplus import NodriverPlus

# show how to raise log level beyond default warnings/errors
async def main():
    logging.basicConfig(level=logging.INFO)
    ndp = NodriverPlus()
    await ndp.start()
    await ndp.stop()

if __name__ == "__main__":
    asyncio.run(main())
