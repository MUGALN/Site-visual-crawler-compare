# site_visual_crawler_compare.py (Sitemap mode)
# Auto-crawl internal links on BASE_URL, screenshot & compare against COMPARE_URL.
# Option A: USE_SITEMAP to take URLs exactly from sitemap(s) instead of BFS.
# Generates an HTML report with Base & Compare images per page and viewport.
# Option 2B (existing): No Diff/Highlight images written, but mismatch % computed in-memory.

import os
import re
import html
import gzip
import asyncio
import unicodedata
import urllib.request as urlrequest
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse, urljoin, unquote
import numpy as np
from PIL import Image, ImageChops
from playwright.async_api import async_playwright, Error as PWError

# ----------------------------- CONFIG ---------------------------------------
BASE_URL = "https://www.kleshchi-by.com"  # <- set to your base site
COMPARE_URL = "https://16751512.livepreview.pfizer"  # <- set to your compare site

# Crawl controls (used only when USE_SITEMAP is False)
START_PATHS = ["/"]  # seeds to start from
MAX_PAGES = 50        # safety cap
MAX_DEPTH = 2         # BFS depth from each seed
KEEP_QUERY = False    # set True to treat ?a=1 and ?a=2 as different pages
EXCLUDE_PATTERNS = [
    r"^/cart",
    r"^/checkout",
    r"^/login",
    r"/admin",
    # FIXED: proper extension alternation
    r"\.(pdf|zip|jpg|jpeg|png|gif|svg|webp|ico)$",
]

# Rendering
VIEWPORTS = [(1366, 768), (390, 844)]
FULL_PAGE = True
WAIT_TIME = 5  # seconds to let UI stabilize
HIDE_SELECTORS = [".cookie", ".cookie-banner", ".chat", ".chat-widget", ".ad"]

# --- Lazy image handling ---
IMAGE_WAIT_MS = 15000  # max time to wait for images per page (ms)
SCROLL_STEP_PX = 600   # scroll step when full_page=True
SCROLL_PAUSE_MS = 150  # pause between scroll steps (ms)

# Browser
HEADLESS = True
BROWSER_CHANNEL = None  # None (Playwright Chromium) or "msedge" or "chrome"
TIMEZONE_ID = "UTC"
LOCALE = "en-US"
FREEZE_TIME_ISO = None  # e.g. "2025-01-01T00:00:00Z" to freeze time

# Output
OUT_DIR = "visual_diff"
IMAGES_DIR = os.path.join(OUT_DIR, "images")

# --- NEW: Sitemap mode (Option A) -------------------------------------------
USE_SITEMAP = True  # when True, skip BFS and use URLs from sitemap(s) exactly
SITEMAP_URLS = [
    # Default guess; change if your sitemap lives elsewhere
    "https://www.kleshchi-by.com/sitemap.xml",
]
# If the sitemap host/protocol differs from BASE_URL (e.g., www vs non-www),
# set this to False to accept all sitemap URLs.
SITEMAP_STRICT_SAME_ORIGIN = True

# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CrawlTarget:
    path: str
    depth: int

def ensure_dirs():
    os.makedirs(IMAGES_DIR, exist_ok=True)

def same_origin(url: str, target_origin: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(target_origin).netloc.lower()

# --- decode %xx and normalize Unicode for paths -----------------------------
def normalize_path(url: str, keep_query: bool) -> str:
    """Return a normalized, human-readable path (decode %xx and NFC-normalize)."""
    u = urlparse(url)
    path = u.path or "/"
    path = unicodedata.normalize("NFC", unquote(path))
    if not keep_query:
        return path
    q = u.query
    return f"{path}?{q}" if q else path

# ---------------------------------------------------------------------------
def is_excluded(path: str, patterns) -> bool:
    for pat in patterns:
        if re.search(pat, path, flags=re.IGNORECASE):
            return True
    return False

# CSS overlay to hide dynamic UI
def overlay_css(hide_selectors):
    css = [
        "* { transition: none !important; animation: none !important; caret-color: transparent !important; }",
        "html { scroll-behavior: auto !important; }",
    ]
    if hide_selectors:
        joined = ", ".join(hide_selectors)
        css.append(f"{joined} {{ visibility: hidden !important; }}")
    return "\n".join(css)

async def freeze_time(context, iso):
    if not iso:
        return
    epoch_ms = int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    await context.add_init_script(f"""
    (function() {{
      const fixed = {epoch_ms};
      const _Date = Date;
      class FixedDate extends _Date {{
        constructor(...args) {{ super(args.length ? ...args : fixed); }}
        static now() {{ return fixed; }}
        static parse(s) {{ return _Date.parse(s); }}
        static UTC(...args) {{ return _Date.UTC(...args); }}
      }}
      Date = FixedDate;
      if (typeof performance !== 'undefined' && performance) {{
        const start = performance.timeOrigin || fixed;
        performance.now = () => 0;
        try {{ Object.defineProperty(performance, 'timeOrigin', {{ value: start }}); }} catch(e) {{}}
      }}
    }})()
    """)

# --- use a.href to get absolute URLs from the DOM (after JS) ----------------
async def extract_links_from_page(page) -> list[str]:
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]",
            "as => as.map(a => a.href).filter(Boolean)"
        )
    except PWError:
        hrefs = []
    return hrefs

# --- Lazy images helpers ----------------------------------------------------
async def force_eager_images(page):
    # Convert <img loading="lazy"> and common data-src patterns to eager
    await page.evaluate("""
    (() => {
      const imgs = Array.from(document.querySelectorAll('img'));
      for (const img of imgs) {
        try {
          img.loading = 'eager';
          if (img.dataset && img.dataset.src && !img.src) img.src = img.dataset.src;
          if (img.dataset && img.dataset.srcset && !img.srcset) img.srcset = img.dataset.srcset;
        } catch {}
      }
    })()
    """)

async def progressive_autoscroll(page, step=600, pause_ms=150):
    # Scroll down in steps to trigger IO/lazy loaders, then back to top
    await page.evaluate(f"""
    async () => {{
      const step = {int(SCROLL_STEP_PX)};
      const pause = {int(SCROLL_PAUSE_MS)};
      const sleep = (ms) => new Promise(r => setTimeout(r, ms));
      let y = 0;
      const max = Math.max(
        document.documentElement.scrollHeight,
        document.body.scrollHeight
      );
      while (y + window.innerHeight < max) {{
        window.scrollTo(0, y);
        await sleep(pause);
        y += step;
      }}
      window.scrollTo(0, 0);
    }}
    """)

async def wait_for_images_complete(page, timeout_ms=15000):
    # Wait until all <img> are loaded (or timeout)
    return await page.evaluate("""
    async (timeout) => {
      const deadline = Date.now() + timeout;
      const sleep = (ms) => new Promise(r => setTimeout(r, ms));
      while (Date.now() < deadline) {
        const imgs = Array.from(document.images || []);
        const pending = imgs.filter(img => !(img.complete && img.naturalWidth > 0));
        if (pending.length === 0) return true;
        await sleep(200);
      }
      return false;
    }
    """, timeout_ms)

async def take_screenshot(page, url: str, full_page: bool, wait_time: float, hide_selectors):
    # Navigate & settle network a bit
    resp = await page.goto(url, wait_until="networkidle", timeout=60_000)
    if resp is None:
        pass
    # Wait for fonts where supported (best-effort)
    try:
        await page.evaluate("(document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve()")
    except PWError:
        pass
    # Stabilize visuals & hide dynamic UI
    try:
        await page.add_style_tag(content=overlay_css(hide_selectors))
    except PWError:
        pass
    # Force-load images and trigger lazy loaders
    await force_eager_images(page)
    if full_page:
        await progressive_autoscroll(page, step=SCROLL_STEP_PX, pause_ms=SCROLL_PAUSE_MS)
    # Optional stabilizing wait
    if wait_time and wait_time > 0:
        await asyncio.sleep(wait_time)
    # Wait until images finish
    try:
        await wait_for_images_complete(page, timeout_ms=IMAGE_WAIT_MS)
    except PWError:
        pass
    # Ensure capture starts at top
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except PWError:
        pass
    return await page.screenshot(full_page=full_page, animations="disabled")

# Pad images for diff metrics

def pad_to_same_size(img1: Image.Image, img2: Image.Image):
    if img1.size == img2.size:
        return img1, img2
    w = max(img1.width, img2.width)
    h = max(img1.height, img2.height)
    bg = (255, 255, 255)
    n1 = Image.new("RGB", (w, h)); n1.paste(img1, (0, 0))
    n2 = Image.new("RGB", (w, h)); n2.paste(img2, (0, 0))
    return n1, n2

# Compute mismatch metrics in-memory (no files)

def compute_mismatch_metrics(base_path, compare_path):
    # Open images safely and compute diff metrics without writing images
    with Image.open(base_path) as _a, Image.open(compare_path) as _b:
        a = _a.convert("RGB")
        b = _b.convert("RGB")
        a, b = pad_to_same_size(a, b)
        diff = ImageChops.difference(a, b)
        diff_arr = np.asarray(diff)
        mismatched_mask = np.any(diff_arr != 0, axis=2)
        mismatched = int(mismatched_mask.sum())
        total = diff_arr.shape[0] * diff_arr.shape[1]
        mismatch_pct = (mismatched / total) * 100 if total else 0.0
        h, w = mismatched_mask.shape
        return {
            "width": w,
            "height": h,
            "mismatchedPixels": mismatched,
            "mismatchPercentage": round(mismatch_pct, 4),
        }

# HTML report

def render_report(meta, results):
    def html_escape(s: str) -> str:
        return html.escape(s, quote=True)

    css = """
    :root { color-scheme: light dark; }
    html, body { margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; }
    h1, h2, h3 { margin: 0.5rem 0; }
    .meta { margin: 12px; color: #666; font-size: 0.95rem; }
    .grid { display: grid; grid-template-columns: 1fr; gap: 16px; padding: 12px; }
    .card { border: none; border-radius: 0; padding: 0; overflow: hidden; }
    .tags { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 8px 0; padding: 0 4px; }
    .tag { background: #f1f5f9; border: 1px solid #e2e8f0; color: #334155; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
    .row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0; }
    .row > div { margin: 0; padding: 0; }
    .row img { width: 100%; height: auto; display: block; border: none; border-radius: 0; background: #fff; }
    .footer { margin: 12px; color: #777; font-size: 0.9rem; }
    """

    rows = []
    for r in results:
        rows.append(f"""
        <div class=\"card\">
          <div class=\"tags\">
            <span class=\"tag\">Path: <strong>{html_escape(r['path'])}</strong></span>
            <span class=\"tag\">Viewport: {r['viewport']}</span>
            <span class=\"tag\">Size: {r['width']}×{r['height']}</span>
            <span class=\"tag\">Diff: {r.get('mismatchPercentage', 0)}% ({r.get('mismatchedPixels', 0)} px)</span>
          </div>
          <div class=\"row\">
            <div>
              <h3 style=\"padding: 4px 8px;\">Base</h3>
              <img loading=\"lazy\" src=\"{html_escape(r['base'])}\">
            </div>
            <div>
              <h3 style=\"padding: 4px 8px;\">Compare</h3>
              <img loading=\"lazy\" src=\"{html_escape(r['compare'])}\">
            </div>
          </div>
        </div>
        """)

    html_doc = f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Visual Comparison Report</title>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>{css}</style>
</head>
<body>
  <h1 style=\"margin:12px;\">Visual Comparison Report</h1>
  <div class=\"meta\">
    <div>Generated: {html_escape(meta['generatedAt'])}</div>
    <div>Base: {html_escape(meta['base'])}</div>
    <div>Compare: {html_escape(meta['compare'])}</div>
    <div>Viewports: {", ".join(html_escape(v) for v in meta['viewports'])}</div>
  </div>
  <div class=\"grid\">
    {"".join(rows)}
  </div>
  <div class=\"footer\">
    Tip: Hide dynamic overlays with selectors (cookie banners, chat). Use Edge/Chrome channels to avoid extra downloads in corporate networks.
  </div>
</body>
</html>"""
    return html_doc

# --- BFS crawler (kept for fallback when USE_SITEMAP=False) -----------------
async def crawl_paths(base_url: str, start_paths: list[str], max_pages: int, max_depth: int,
                      keep_query: bool, exclude_patterns: list[str]) -> list[str]:
    """
    Use Playwright to crawl the base site (so JS-rendered links are included).
    Returns a sorted list of unique paths discovered.
    """
    discovered: set[str] = set()
    origin = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(base_url))

    async with async_playwright() as p:
        launch_kwargs = {"headless": HEADLESS}
        if BROWSER_CHANNEL:
            launch_kwargs["channel"] = BROWSER_CHANNEL
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            timezone_id=TIMEZONE_ID,
            locale=LOCALE,
        )
        page = await context.new_page()

        q = deque()
        # seed queue with normalized targets
        for sp in start_paths:
            abs_url = urljoin(base_url, sp)
            path = normalize_path(abs_url, keep_query)
            if not is_excluded(path, exclude_patterns):
                q.append(CrawlTarget(path=path, depth=0))
                discovered.add(path)

        while q and len(discovered) < max_pages:
            current = q.popleft()
            abs_url = urljoin(origin, current.path)
            try:
                await page.goto(abs_url, wait_until="networkidle", timeout=60_000)
                try:
                    await page.evaluate("(document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve()")
                except PWError:
                    pass
            except PWError:
                # skip broken pages
                continue

            # Extract links & enqueue
            try:
                links = await extract_links_from_page(page)
            except PWError:
                links = []

            for href in links:
                if not same_origin(href, origin):
                    continue
                path = normalize_path(href, keep_query)
                if is_excluded(path, exclude_patterns):
                    continue
                if path not in discovered:
                    if current.depth + 1 <= max_depth and len(discovered) < max_pages:
                        discovered.add(path)
                        q.append(CrawlTarget(path=path, depth=current.depth + 1))

        await context.close()
        await browser.close()

    # Prefer shorter (path-only) sort
    return sorted(discovered, key=lambda s: (s.count("/"), s))

# --- NEW: Simple sitemap parser (supports index + gzip) ---------------------

def _et_findall_ns(elem, path):
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return elem.findall(path, namespaces=ns)

def _fetch_bytes(url: str, timeout=60):
    with urlrequest.urlopen(url, timeout=timeout) as resp:
        return resp.read()

def _iter_sitemap_urls(url: str):
    data = _fetch_bytes(url)
    if url.lower().endswith(".gz"):
        data = gzip.decompress(data)
    root = ET.fromstring(data)
    tag = root.tag.lower()
    if tag.endswith("sitemapindex"):
        for loc in _et_findall_ns(root, ".//sm:sitemap/sm:loc"):
            child = (loc.text or "").strip()
            if child:
                yield from _iter_sitemap_urls(child)
    else:
        for loc in _et_findall_ns(root, ".//sm:url/sm:loc"):
            u = (loc.text or "").strip()
            if u:
                yield u

def paths_from_sitemaps(base_url: str, sitemap_urls: list[str], keep_query: bool, strict_same_origin=True) -> list[str]:
    origin = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(base_url))
    seen = set()
    for sm in sitemap_urls:
        try:
            for abs_u in _iter_sitemap_urls(sm):
                if strict_same_origin and not same_origin(abs_u, origin):
                    continue
                path = normalize_path(abs_u, keep_query)
                if not is_excluded(path, EXCLUDE_PATTERNS):
                    seen.add(path)
        except Exception as e:
            print("! Sitemap read failed:", sm, e)
            continue
    return sorted(seen, key=lambda s: (s.count("/"), s))

# --- robust filename sanitizer (ASCII) --------------------------------------

def filename_from_path(path: str) -> str:
    """
    Create a filesystem-safe ASCII filename from a URL path (no directories).
    Keeps '.', '_', '-', and alphanumerics. Converts others to underscores.
    """
    stem = path.strip("/").replace("/", "_") or "home"
    # Normalize and transliterate to ASCII
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = stem.strip("_") or "home"
    return stem

# ---------------------------------------------------------------------------
async def run_compare():
    ensure_dirs()
    meta = {
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "base": BASE_URL,
        "compare": COMPARE_URL,
        "viewports": [f"{w}x{h}" for (w, h) in VIEWPORTS],
        "options": {
            "fullPage": FULL_PAGE,
            "headless": HEADLESS,
            "timezone": TIMEZONE_ID,
            "locale": LOCALE,
            "freezeTime": FREEZE_TIME_ISO,
            "hide": HIDE_SELECTORS,
            "browserChannel": BROWSER_CHANNEL or "playwright-bundled",
            "maxPages": MAX_PAGES,
            "maxDepth": MAX_DEPTH,
            "keepQuery": KEEP_QUERY,
            "excludes": EXCLUDE_PATTERNS,
            "sitemapMode": USE_SITEMAP,
            "sitemaps": SITEMAP_URLS,
        },
    }

    if USE_SITEMAP and SITEMAP_URLS:
        print("🧭 Using sitemap URLs (no crawling):", ", ".join(SITEMAP_URLS))
        paths = paths_from_sitemaps(
            base_url=BASE_URL,
            sitemap_urls=SITEMAP_URLS,
            keep_query=KEEP_QUERY,
            strict_same_origin=SITEMAP_STRICT_SAME_ORIGIN,
        )
    else:
        print("🔎 Crawling paths from BASE:", BASE_URL)
        paths = await crawl_paths(
            base_url=BASE_URL,
            start_paths=START_PATHS,
            max_pages=MAX_PAGES,
            max_depth=MAX_DEPTH,
            keep_query=KEEP_QUERY,
            exclude_patterns=EXCLUDE_PATTERNS,
        )

    if not paths:
        print("No paths discovered. Check your BASE_URL, SITEMAP_URLS, and EXCLUDE_PATTERNS.")
        return

    print(f"Found {len(paths)} path(s):")
    for p in paths:
        print(" •", p)

    results = []

    async with async_playwright() as p:
        launch_kwargs = {"headless": HEADLESS}
        if BROWSER_CHANNEL:
            launch_kwargs["channel"] = BROWSER_CHANNEL
        browser = await p.chromium.launch(**launch_kwargs)

        # Per-viewport contexts
        for (width, height) in VIEWPORTS:
            ctx_base = await browser.new_context(
                viewport={"width": width, "height": height},
                timezone_id=TIMEZONE_ID,
                locale=LOCALE,
            )
            ctx_compare = await browser.new_context(
                viewport={"width": width, "height": height},
                timezone_id=TIMEZONE_ID,
                locale=LOCALE,
            )
            await freeze_time(ctx_base, FREEZE_TIME_ISO)
            await freeze_time(ctx_compare, FREEZE_TIME_ISO)

            page_base = await ctx_base.new_page()
            page_compare = await ctx_compare.new_page()

            for path in paths:
                vp_slug = f"{width}x{height}"
                fname_safe = filename_from_path(path)
                base_url = urljoin(BASE_URL, path)
                compare_url = urljoin(COMPARE_URL, path)

                base_out = os.path.join(IMAGES_DIR, f"{fname_safe}_{vp_slug}_base.png")
                compare_out = os.path.join(IMAGES_DIR, f"{fname_safe}_{vp_slug}_compare.png")

                print(f"[{vp_slug}] {path}")
                print("  BASE   ", base_url)
                print("  COMPARE", compare_url)

                try:
                    base_buf = await take_screenshot(page_base, base_url, FULL_PAGE, WAIT_TIME, HIDE_SELECTORS)
                    with open(base_out, "wb") as f:
                        f.write(base_buf)
                except PWError as e:
                    print("  ! Base screenshot failed:", e)
                    continue

                try:
                    cmp_buf = await take_screenshot(page_compare, compare_url, FULL_PAGE, WAIT_TIME, HIDE_SELECTORS)
                    with open(compare_out, "wb") as f:
                        f.write(cmp_buf)
                except PWError as e:
                    print("  ! Compare screenshot failed:", e)
                    continue

                # Compute mismatch metrics in-memory (no diff/highlight files)
                metrics = compute_mismatch_metrics(base_out, compare_out)
                results.append({
                    "path": path,
                    "viewport": vp_slug,
                    "base": os.path.relpath(base_out, OUT_DIR).replace("\\", "/"),
                    "compare": os.path.relpath(compare_out, OUT_DIR).replace("\\", "/"),
                    **metrics,
                })

            await ctx_base.close()
            await ctx_compare.close()

        await browser.close()

    # Write report
    report_html = render_report(meta, results)
    report_path = os.path.join(OUT_DIR, "report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    print(f"\n✅ Done! Open: {report_path}")

if __name__ == "__main__":
    asyncio.run(run_compare())
