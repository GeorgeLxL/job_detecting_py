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
    if not CONFIG_FILE.exists():
        sys.exit(f"[ERROR] Config file not found: {CONFIG_FILE.resolve()}\n"
                 f"        Copy config.json.example to config.json and edit it.")
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"[ERROR] Invalid JSON in config.json: {e}")

    # Validate ntfy topic.
    topic = cfg.get("ntfy", {}).get("topic", "")
    if not topic or "CHANGE-THIS" in topic:
        sys.exit("[ERROR] Please set a unique ntfy topic in config.json.\n"
                 "        Then subscribe at https://ntfy.sh/<your-topic> on Windows.")

    # Validate time strings if alwaysOn is false.
    tw = cfg.get("timeWindow", {})
    if not tw.get("alwaysOn", False):
        for key in ("from", "to"):
            val = tw.get(key, "")
            try:
                h, m = map(int, val.split(":"))
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except (ValueError, AttributeError):
                sys.exit(f"[ERROR] timeWindow.{key} is invalid: '{val}'. Use HH:MM format.")

    # Validate site modes.
    for i, site in enumerate(cfg.get("sites", [])):
        mode = site.get("mode", "NEW")
        if mode not in ("NEW", "ANY"):
            sys.exit(f"[ERROR] sites[{i}].mode is '{mode}'. Must be 'NEW' or 'ANY'.")

    return cfg


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] Could not read state file ({e}). Starting fresh.")
    return {"seen": {}}


def save_state(state):
    try:
        STATE_FILE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        print(f"[ERROR] Could not save state: {e}. Seen-jobs progress may be lost.")


def is_within_window(tw):
    # Default alwaysOn to False so from/to are respected when provided.
    if not tw or tw.get("alwaysOn", False):
        return True
    now     = datetime.now()
    now_min = now.hour * 60 + now.minute
    def to_min(s):
        h, m = map(int, s.split(":"))
        return h * 60 + m
    f = to_min(tw.get("from", "00:00"))
    t = to_min(tw.get("to",   "23:59"))
    if f <= t:
        return f <= now_min <= t
    return now_min >= f or now_min <= t     # overnight window


def short_url(url):
    try:
        u = urlparse(url)
        return u.netloc + u.path
    except Exception:
        return url


# ---------------------------------------------------------------------------
# ntfy notification (shared client, response status checked)
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None

async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=10)
    return _http_client

async def notify(cfg, title, message, priority=3):
    ntfy  = cfg.get("ntfy", {})
    base  = ntfy.get("url", "https://ntfy.sh").rstrip("/")
    topic = ntfy.get("topic", "CHANGE-THIS-TOPIC")
    headers = {
        "Title":    title,
        "Priority": str(priority),
        "Tags":     "briefcase",
    }
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        client = await get_http_client()
        r = await client.post(f"{base}/{topic}", content=message.encode(), headers=headers)
        r.raise_for_status()
        print(f"[{ts}] NOTIFY  {title}: {message}")
    except httpx.HTTPStatusError as e:
        print(f"[{ts}] NOTIFY FAILED (HTTP {e.response.status_code}): {e}")
    except Exception as e:
        print(f"[{ts}] NOTIFY ERROR: {e}")


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
        await page.wait_for_selector(selector, timeout=8000)
        tab_els = await page.query_selector_all(selector)
        for el in tab_els:
            label = (await el.inner_text()).strip().lower()
            if target_text in label:
                await el.click()
                # Wait for content to settle after tab switch.
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except PWTimeout:
                    await asyncio.sleep(1)
                print(f"  [TAB] Activated: {(await el.inner_text()).strip()}")
                return
        print(f"  [TAB] WARNING: tab matching '{target_text}' not found.")
    except PWTimeout:
        print(f"  [TAB] Timed out waiting for tab selector '{selector}'.")
    except Exception as e:
        print(f"  [TAB] Error: {e}")


# ---------------------------------------------------------------------------
# Core scan (one site, one page)
# ---------------------------------------------------------------------------

async def scan_site(page, site, state, cfg):
    url      = site.get("url", "")
    keywords = site.get("containerClassKeywords", "")
    mode     = site.get("mode", "NEW")
    tab_cfg  = site.get("targetTab")
    # Key includes name so two sites at same URL stay separate.
    site_key = f"{url}::{site.get('name', url)}"

    # Reset page to blank first so a previous failed navigation doesn't bleed through.
    try:
        await page.goto("about:blank", timeout=5000)
    except Exception:
        pass

    print(f"  Navigating to {short_url(url)} ...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Wait for main content to be present rather than fixed sleep.
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PWTimeout:
        print("  [WARN] networkidle timed out — continuing with current DOM state.")
    except Exception as e:
        await notify(cfg, "Page load failed", f"{short_url(url)}: {e}")
        return

    await activate_tab(page, tab_cfg)
    if tab_cfg:
        # Wait for list container to appear after tab click rather than fixed sleep.
        if keywords.strip():
            kw = keywords.strip().split()[0]
            try:
                await page.wait_for_selector(f"[class*='{kw}']", timeout=5000)
            except PWTimeout:
                pass

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
    # Reload state from disk each cycle so external edits (e.g. manual baseline reset) take effect.
    fresh_state  = load_state()
    seen         = fresh_state.setdefault("seen", {}).setdefault(site_key, {})
    new_ids      = [i for i in ids if i and i not in seen]

    now_ts = int(time.time())
    for i in ids:
        if i:
            seen[i] = now_ts

    # Trim oldest beyond 1000 per site.
    if len(seen) > 1000:
        oldest = sorted(seen, key=seen.__getitem__)[:len(seen) - 1000]
        for k in oldest:
            del seen[k]

    # Propagate changes back to the in-memory state dict and persist.
    state["seen"] = fresh_state["seen"]
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
    interval = max(1, int(cfg.get("interval", 5)))
    headless = cfg.get("headless", False)

    if int(cfg.get("interval", 5)) < 1:
        print("[WARN] Interval below 1 minute is not supported. Using 1 minute.")

    print(f"Job Monitor — interval={interval}min  sites={len(sites)}")
    print(f"ntfy topic : {cfg.get('ntfy', {}).get('topic')}")
    print(f"Profile dir: {PROFILE_DIR.resolve()}")
    if not headless:
        print("Browser is VISIBLE — log in if needed, then leave it open.")

    PROFILE_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        ctx  = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        # Always use a fresh page to avoid inheriting state from the profile's last session.
        page = await ctx.new_page()

        try:
            while True:
                if not is_within_window(cfg.get("timeWindow")):
                    ts = datetime.now().strftime("%H:%M")
                    print(f"[{ts}] Outside time window — sleeping {interval}min.")
                else:
                    for site in sites:
                        if not site.get("url"):
                            continue
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"\n[{ts}] SCAN: {site.get('name', site['url'])}")
                        try:
                            await scan_site(page, site, state, cfg)
                        except Exception as e:
                            print(f"  [ERROR] Unexpected: {e}")

                await asyncio.sleep(interval * 60)

        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nShutting down.")
        finally:
            if _http_client and not _http_client.is_closed:
                await _http_client.aclose()
            await ctx.close()


if __name__ == "__main__":
    asyncio.run(run())
