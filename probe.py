"""
One-shot probe: opens each site, activates the configured tab,
and reports whether the container was found and how many cards.
Use this to verify containerClassKeywords before running monitor.py.

Run: python probe.py
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from monitor import load_config, activate_tab, EXTRACT_JS, short_url, PROFILE_DIR


async def probe():
    cfg   = load_config()
    sites = cfg.get("sites", [])

    print("=== PROBE MODE ===")
    print("Browser will open. Log in if prompted, then watch the output.\n")

    async with async_playwright() as p:
        ctx  = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await ctx.new_page()

        for site in sites:
            url      = site.get("url", "")
            keywords = site.get("containerClassKeywords", "")
            tab_cfg  = site.get("targetTab")

            print(f"--- {site.get('name', url)} ---")
            print(f"  URL      : {url}")
            print(f"  Keywords : {keywords!r}")
            if tab_cfg:
                print(f"  Tab      : '{tab_cfg.get('text')}' via {tab_cfg.get('selector')}")

            # Navigate.
            try:
                await page.goto("about:blank", timeout=5000)
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except PWTimeout:
                    print("  [WARN] networkidle timed out — continuing.")
            except PWTimeout:
                print("  [ERROR] Navigation timed out. Skipping site.\n")
                continue
            except Exception as e:
                print(f"  [ERROR] Navigation failed: {e}. Skipping site.\n")
                continue

            # Activate target tab.
            await activate_tab(page, tab_cfg)
            if tab_cfg and keywords.strip():
                kw = keywords.strip().split()[0]
                try:
                    await page.wait_for_selector(f"[class*='{kw}']", timeout=5000)
                except PWTimeout:
                    pass

            # Run scan.
            try:
                result = await page.evaluate(EXTRACT_JS, keywords)
            except Exception as e:
                print(f"  [ERROR] JS evaluation failed: {e}\n")
                continue

            found = result.get("found", False)
            count = result.get("count", 0)
            ids   = result.get("ids", [])

            print(f"  Result   : found={found}  count={count}")
            if found and ids:
                print(f"  Sample IDs ({min(5, len(ids))}/{len(ids)}):")
                for id_ in ids[:5]:
                    print(f"    {id_}")

            if not found:
                print()
                print("  HINT: Open DevTools on this page (F12), inspect the")
                print("  element that wraps ALL job cards, and copy some of its")
                print("  class names into config.json > containerClassKeywords.")
            print()

        input("Press ENTER to close browser.")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(probe())
