"""
One-shot probe: opens each site, activates the configured tab,
and tells you whether the container was found and how many cards.
Use this to find and verify containerClassKeywords before running monitor.py.

Run: python probe.py
"""
import asyncio
import json
from pathlib import Path
from monitor import load_config, activate_tab, EXTRACT_JS, short_url, PROFILE_DIR

async def probe():
    cfg   = load_config()
    sites = cfg.get("sites", [])

    print("=== PROBE MODE ===")
    print("Browser will open. Navigate / log in if needed.")
    print()

    async with __import__("playwright.async_api", fromlist=["async_playwright"]).async_playwright() as p:
        ctx  = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        for site in sites:
            url      = site.get("url", "")
            keywords = site.get("containerClassKeywords", "")
            tab_cfg  = site.get("targetTab")

            print(f"--- {site.get('name', url)} ---")
            print(f"  URL      : {url}")
            print(f"  Keywords : {keywords}")
            if tab_cfg:
                print(f"  Tab      : '{tab_cfg.get('text')}' via {tab_cfg.get('selector')}")

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2)
            await activate_tab(page, tab_cfg)
            if tab_cfg:
                await asyncio.sleep(1)

            result = await page.evaluate(EXTRACT_JS, keywords)
            print(f"  Result   : found={result['found']}  count={result['count']}")
            if result["found"] and result["ids"]:
                print(f"  Sample IDs: {result['ids'][:5]}")
            if not result["found"]:
                print()
                print("  HINT: Open DevTools on the page, find the job list container,")
                print("  and copy some of its class names into config.json.")
            print()

        input("Press ENTER to close browser.")
        await ctx.close()

if __name__ == "__main__":
    asyncio.run(probe())
