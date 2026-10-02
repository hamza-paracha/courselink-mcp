import asyncio
from .browser import BrowserSession
from .config import Config


async def login():
    browser = BrowserSession(Config.load())
    try:
        await browser.start()
        await browser.open_login()
        print('Sign in using the separate Chromium window. This window closes after the session is saved.', flush=True)
        while True:
            status = await browser.keepalive()
            if status['state'] == 'authenticated':
                print('CourseLink session saved. Ready to start the service.', flush=True)
                return
            await asyncio.sleep(5)
    finally:
        await browser.close()


if __name__ == '__main__':
    asyncio.run(login())
