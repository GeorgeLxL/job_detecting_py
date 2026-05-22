"""
Job Monitor - Ubuntu edition
Notifies your local machine via ntfy.sh (https://ntfy.sh)
Run: python monitor.py
"""
import asyncio
import json
import time
import sys
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

CONFIG_FILE = Path("config.json")
STATE_FILE  = Path("state.json")
PROFILE_DIR = Path("browser_profile")

# ---------------------------------------------------------------------------
# JS injected into each page to locate the container and extract card IDs.
# Same robust descent + stable-ID logic as the Chrome extension (Project1).
# ---------------------------------------------------------------------------
EXTRACT_JS = r"""
(keywords) => {
  function findContainer(kws) {
    for (const el of document.querySelectorAll('*')) {
      if (!el.classList || !el.classList.length) continue;
      if (kws.every(k => el.classList.contains(k))) return el;
    }
    return null;
  }

  function findCards(container, maxDepth) {
    if (!container) return [];
    maxDepth = maxDepth || 4;
    let node = container;
    for (let d = 0; d < maxDepth; d++) {
      const kids = Array.from(node.children);
      if (!kids.length) return [];
      const sig = el => el.tagName + '|' + (el.getAttribute('class') || '');
      const counts = {};
      for (const k of kids) counts[sig(k)] = (counts[sig(k)] || 0) + 1;
      let topSig = null, topCount = 0;
      for (const [s, c] of Object.entries(counts)) {
        if (c > topCount) { topSig = s; topCount = c; }
      }
      if (topCount >= 2) return kids.filter(k => sig(k) === topSig);
      if (kids.length === 1) { node = kids[0]; continue; }
      let best = kids[0], bestScore = -1;
      for (const k of kids) {
        const s = k.querySelectorAll('*').length;
        if (s > bestScore) { bestScore = s; best = k; }
      }
      node = best;
    }
    return Array.from(node.children);
  }

  function normalizeText(s) {
    let t = (s || '').toLowerCase();
    t = t.replace(/\d+\s*(秒|分|時間|日|週間|ヶ?月|年)\s*前/g, '');
    t = t.replace(/\d+\s*(second|minute|hour|day|week|month|year)s?\s*ago/g, '');
    t = t.replace(/\d[\d,.:\-\/]*/g, '');
    return t.replace(/\s+/g, ' ').trim().slice(0, 240);
  }

  function hashStr(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return 't_' + (h >>> 0).toString(36);
  }

  function extractId(card) {
    for (const attr of ['data-id', 'data-job-id', 'data-task-id', 'data-item-id']) {
      const v = card.getAttribute(attr);
      if (v) return 'd_' + v;
    }
    if (card.id) return 'i_' + card.id;
    const links = card.tagName === 'A' ? [card]
                  : Array.from(card.querySelectorAll('a[href]'));
    for (const link of links) {
      const href = link.getAttribute('href');
      if (!href || href.startsWith('#') || href.startsWith('javascript:')) continue;
      try {
        const u = new URL(href, location.href);
        if (u.pathname && u.pathname !== '/') return 'h_' + u.host + u.pathname;
      } catch {}
    }
    return hashStr(normalizeText(card.innerText || card.textContent || ''));
  }

  const kws = (keywords || '').trim().split(/\s+/).filter(Boolean);
  const container = findContainer(kws);
  if (!container) return { found: false, count: 0, ids: [] };
  const cards = findCards(container);
  return { found: true, count: cards.length, ids: cards.map(extractId) };
}
"""


# ---------------------------------------------------------------------------
# Config / state helpers
# ---------------------------------------------------------------------------

def load_config():
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": {}}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def is_within_window(tw):
    if not tw or tw.get("alwaysOn", True):
        return True
    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    def to_min(s):
        h, m = map(int, s.split(":"))
        return h * 60 + m
    f = to_min(tw.get("from", "00:00"))
    t = to_min(tw.get("to", "23:59"))
    if f <= t:
        return f <= now_min <= t
    return now_min >= f or now_min <= t    # overnight

def short_url(url):
    try:
        u = urlparse(url)
        return u.netloc + u.path
    except Exception:
        return url


# ---------------------------------------------------------------------------
# ntfy notification
# ---------------------------------------------------------------------------

async def notify(cfg, title, message, priority=3):
    ntfy = cfg.get("ntfy", {})
    base  = ntfy.get("url", "https://ntfy.sh").rstrip("/")
    topic = ntfy.get("topic", "job-monitor")
    headers = {
        "Title":    title,
        "Priority": str(priority),
        "Tags":     "briefcase",
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{base}/{topic}", content=message.encode(),
                              headers=headers, timeout=10)
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] NOTIFY  {title}: {message}")
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")


# ---------------------------------------------------------------------------
# Tab activation (handles same-URL, multiple page sections)
# ---------------------------------------------------------------------------

async def activate_tab(page, tab_cfg):
    """Click the in-page tab whose text matches tab_cfg['text']."""
    if not tab_cfg:
        return
    selector    = tab_cfg.get("selector", "[role='tab']")
    target_text = tab_cfg.get("text", "").strip().lower()
    if not target_text:
        return
    try:
        # Wait a moment for tabs to render in SPAs.
        await page.wait_for_selector(selector, timeout=8000)
        tab_els = await page.query_selector_all(selector)
        for el in tab_els:
            label = (await el.inner_text()).strip().lower()
            if target_text in label:
                await el.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except PWTimeout:
                    await asyncio.sleep(1.5)
                print(f"  [TAB] Activated: {await el.inner_text()}")
                return
        print(f"  [TAB] WARNING: tab matching '{target_text}' not found.")
    except Exception as e:
        print(f"  [TAB] Error activating tab: {e}")


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

async def scan_site(page, site, state, cfg):
    url      = site.get("url", "")
    keywords = site.get("containerClassKeywords", "")
    mode     = site.get("mode", "NEW")
    tab_cfg  = site.get("targetTab")
    # State key includes name so two sites at same URL stay separate.
    site_key = f"{url}::{site.get('name', url)}"

    print(f"  Navigating to {short_url(url)} ...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)          # let SPA hydrate
    except Exception as e:
        await notify(cfg, "Page load failed", f"{short_url(url)}: {e}")
        return

    # Click the correct in-page tab if configured.
    await activate_tab(page, tab_cfg)
    if tab_cfg:
        await asyncio.sleep(1)          # settle after tab switch

    if not keywords.strip():
        print("  [SKIP] No containerClassKeywords configured.")
        return

    try:
        result = await page.evaluate(EXTRACT_JS, keywords)
    except Exception as e:
        await notify(cfg, "Scan error", f"{short_url(url)}: {e}")
        return

    found = result.get("found", False)
    count = result.get("count", 0)
    ids   = result.get("ids", [])

    print(f"  found={found}  count={count}  mode={mode}")

    if not found:
        await notify(cfg, "Container not found",
                     f'No element matched "{keywords}" on {short_url(url)}.')
        return

    if count == 0:
        await notify(cfg, "No jobs detected",
                     f"Container found on {short_url(url)} but it is empty.")
        return

    if mode == "ANY":
        await notify(cfg, "Jobs available",
                     f"{count} job{'s' if count != 1 else ''} on {short_url(url)}.",
                     priority=4)
        return

    # NEW mode — identity diff.
    seen    = state.setdefault("seen", {}).setdefault(site_key, {})
    new_ids = [i for i in ids if i and i not in seen]

    now_ts = int(time.time())
    for i in ids:
        if i:
            seen[i] = now_ts
    # Trim oldest beyond 1000 per site.
    if len(seen) > 1000:
        oldest = sorted(seen, key=seen.__getitem__)[:len(seen) - 1000]
        for k in oldest:
            del seen[k]
    save_state(state)

    if new_ids:
        await notify(cfg, "New jobs available",
                     f"{len(new_ids)} new job{'s' if len(new_ids) != 1 else ''}"
                     f" on {short_url(url)} (total {count}).",
                     priority=5)
    else:
        print(f"  [OK] {count} jobs, 0 new.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run():
    cfg      = load_config()
    state    = load_state()
    sites    = cfg.get("sites", [])
    interval = max(1, cfg.get("interval", 5)) * 60
    headless = cfg.get("headless", False)

    print(f"Job Monitor — interval={cfg.get('interval',5)}min  sites={len(sites)}")
    print(f"ntfy topic : {cfg.get('ntfy', {}).get('topic', '(not set)')}")
    print(f"Profile dir: {PROFILE_DIR.resolve()}")
    if not headless:
        print("Browser is VISIBLE — log in if needed, then leave it open.")

    PROFILE_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            while True:
                if not is_within_window(cfg.get("timeWindow")):
                    print(f"[{datetime.now().strftime('%H:%M')}] Outside time window — sleeping.")
                else:
                    for site in sites:
                        if not site.get("url"):
                            continue
                        print(f"\n[SCAN] {site.get('name', site['url'])}")
                        try:
                            await scan_site(page, site, state, cfg)
                        except Exception as e:
                            print(f"  [ERROR] {e}")
                await asyncio.sleep(interval)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nShutting down.")
        finally:
            await ctx.close()


if __name__ == "__main__":
    asyncio.run(run())
