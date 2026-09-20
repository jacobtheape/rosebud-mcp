"""Headless-browser lifecycle for live checkout.

Playwright is imported lazily so the default fake checkout mode works
without playwright installed. Live mode needs:
    pip install playwright && playwright install chromium
"""

from contextlib import asynccontextmanager


@asynccontextmanager
async def browser_page(headless: bool = True):
    """Yield a fresh Playwright page, closing the browser on exit."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(
            "Live checkout needs playwright: "
            "pip install playwright && playwright install chromium"
        ) from e
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = await context.new_page()
            yield page
        finally:
            await browser.close()
