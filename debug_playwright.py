import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            page = context.pages[0]
            print(f"URL: {page.url}")
            
            elements = await page.locator("div.ds-markdown").all()
            print(f"Found {len(elements)} div.ds-markdown elements")
            
            for i, el in enumerate(elements[-3:]):
                text = await el.inner_text()
                print(f"Element {-len(elements[-3:])+i} text: {text[-200:]!r}")
        except Exception as e:
            print(f"Error: {e}")

asyncio.run(main())
