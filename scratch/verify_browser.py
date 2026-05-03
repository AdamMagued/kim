import asyncio
import httpx
from playwright.async_api import async_playwright

async def verify_browser():
    cdp_url = "http://localhost:9222"
    print(f"Checking for Chrome at {cdp_url}...")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{cdp_url}/json/version")
            print(f"Connection Successful: {resp.json().get('Browser', 'Unknown Browser')}")
    except Exception as e:
        print(f"Failed to connect to Chrome. Error: {e}")
        print("Make sure you launched Chrome with --remote-debugging-port=9222")
        return

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            # context = browser.contexts[0]
            # pages = context.pages
            
            # Better way to get pages in CDP
            pages = []
            for context in browser.contexts:
                pages.extend(context.pages)
                
            print(f"Found {len(pages)} open tabs.")
            
            chat_tabs = []
            for page in pages:
                url = page.url
                if any(site in url for site in ["claude.ai", "gemini.google.com", "chatgpt.com", "deepseek.com"]):
                    try:
                        title = await page.title()
                        chat_tabs.append((title, url))
                    except:
                        chat_tabs.append(("Unknown Title", url))
            
            if chat_tabs:
                print("\nActive Chat Tabs:")
                for title, url in chat_tabs:
                    print(f" - {title}: {url}")
            else:
                print("\nNo AI chat tabs found. Please open Claude, Gemini, or ChatGPT.")
                
            await browser.close()
        except Exception as e:
            print(f"Error during tab inspection: {e}")

if __name__ == "__main__":
    asyncio.run(verify_browser())
