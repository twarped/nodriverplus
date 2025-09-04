import asyncio
from nodriverplus import NodriverPlus

# basic stealth startup + shutdown
async def main():
    ndp = NodriverPlus()  # hide_headless defaults to True
    # start browser (headless False by default for now) - adjust as needed
    await ndp.start()
    # do nothing - just demonstrate bring-up
    await ndp.stop()  # graceful (waits for process)

if __name__ == "__main__":
    asyncio.run(main())
