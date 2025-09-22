# site_visual_crawler_compare.py
# Auto-crawl internal links on BASE_URL, screenshot & compare against COMPARE_URL.
# Generates an HTML report with Base / Compare / Diff images per page and viewport.

import os
import re
import asyncio
from PIL import ImageDraw
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse, urljoin, urlunparse

import numpy as np
from PIL import Image, ImageChops
from playwright.async_api import async_playwright, Error as PWError

# ------------------------ CONFIG ------------------------
BASE_URL = "https://www.comirnaty.cz/"     # <- change me
COMPARE_URL = "https://16700102.livepreview.pfizer/"  # <- change me

# Crawl controls
START_PATHS = ["/"]           # seeds to start from
MAX_PAGES = 50                # safety cap
MAX_DEPTH = 2                 # BFS depth from each seed
KEEP_QUERY = False            # set True to treat ?a=1 and ?a=2 as different pages
EXCLUDE_PATTERNS = [
    r"^/cart",
    r"^/checkout",
    r"^/login",
    r"/admin",
    r"\.(pdf|zip|jpg|jpeg|png|gif|svg|webp|ico)$",
]

# Rendering
VIEWPORTS = [(1366, 768), (390, 844)]
FULL_PAGE = True
WAIT_TIME = 0.3               # seconds to let UI stabilize
HIDE_SELECTORS = [".cookie", ".cookie-banner", ".chat", ".chat-widget", ".ad"]

# Browser
HEADLESS = True
BROWSER_CHANNEL = None        # None (Playwright Chromium) or "msedge" or "chrome"
TIMEZONE_ID = "UTC"
LOCALE = "en-US"
FREEZE_TIME_ISO = None        # e.g. "2025-01-01T00:00:00Z" to freeze time

# Output
OUT_DIR = "visual_diff"
IMAGES_DIR = os.path.join(OUT_DIR, "images")
# --------------------------------------------------------


@dataclass(frozen=True)
class CrawlTarget:
    path: str
    depth: int


def ensure_dirs():
    os.makedirs(IMAGES_DIR, exist_ok=True)


def same_origin(url: str, target_origin: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(target_origin).netloc.lower()


def normalize_path(url: str, keep_query: bool) -> str:
    """Return a normalized path string (for set membership & filenames)."""
    u = urlparse(url)
    path = u.path or "/"
    if not keep_query:
        return path
    # Include query (but never fragment)
    q = u.query
    if q:
        return f"{path}?{q}"
    return path


def is_excluded(path: str, patterns) -> bool:
    for pat in patterns:
        if re.search(pat, path, flags=re.IGNORECASE):
            return True
    return False


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
      }})();
    """)


async def extract_links_from_page(page) -> list[str]:
    """Return absolute hrefs from <a> elements as seen in the DOM (after JS)."""
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]",
            "as => as.map(a => a.getAttribute('href')).filter(Boolean)"
        )
    except PWError:
        hrefs = []
    # Resolve potential relative hrefs using page.url on the Python side
    base = page.url
    abs_urls = []
    for href in hrefs:
        # Skip anchors
        if href.startswith("#"):
            continue
        # Resolve
        abs_url = urljoin(base, href)
        abs_urls.append(abs_url)
    return abs_urls


async def take_screenshot(page, url: str, full_page: bool, wait_time: float, hide_selectors):
    resp = await page.goto(url, wait_until="networkidle", timeout=60_000)
    if resp is None:
        # could be non-navigation (e.g., about:blank)
        pass
    # Wait for fonts where supported
    try:
        await page.evaluate("(document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve()")
    except PWError:
        pass
    # Stabilize visuals & hide dynamic UI
    try:
        await page.add_style_tag(content=overlay_css(hide_selectors))
    except PWError:
        pass
    if wait_time and wait_time > 0:
        await asyncio.sleep(wait_time)
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except PWError:
        pass
    return await page.screenshot(full_page=full_page, animations="disabled")


def pad_to_same_size(img1: Image.Image, img2: Image.Image):
    if img1.size == img2.size:
        return img1, img2
    w = max(img1.width, img2.width)
    h = max(img1.height, img2.height)
    bg = (255, 255, 255)
    n1 = Image.new("RGB", (w, h), bg); n1.paste(img1, (0, 0))
    n2 = Image.new("RGB", (w, h), bg); n2.paste(img2, (0, 0))
    return n1, n2


def diff_and_metrics(base_path, compare_path, diff_path, highlight_path):
    a = Image.open(base_path).convert("RGB")
    b = Image.open(compare_path).convert("RGB")
    a, b = pad_to_same_size(a, b)
    diff = ImageChops.difference(a, b)
    diff.save(diff_path)

    diff_arr = np.asarray(diff)
    mismatched_mask = np.any(diff_arr != 0, axis=2)
    mismatched = int(mismatched_mask.sum())
    total = diff_arr.shape[0] * diff_arr.shape[1]
    mismatch_pct = (mismatched / total) * 100 if total else 0.0

    # Highlight differences
    highlight_img = b.copy()
    draw = ImageDraw.Draw(highlight_img)

    visited = np.zeros_like(mismatched_mask, dtype=bool)
    h, w = mismatched_mask.shape

    def bfs(y, x):
        queue = [(y, x)]
        visited[y, x] = True
        min_x, max_x, min_y, max_y = x, x, y, y
        while queue:
            cy, cx = queue.pop(0)
            for dy in [-1, 0, 1]:
                for dx in [-1, 0, 1]:
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and mismatched_mask[ny, nx]:
                        visited[ny, nx] = True
                        queue.append((ny, nx))
                        min_x = min(min_x, nx)
                        max_x = max(max_x, nx)
                        min_y = min(min_y, ny)
                        max_y = max(max_y, ny)
        return min_x, min_y, max_x, max_y

    for y in range(h):
        for x in range(w):
            if mismatched_mask[y, x] and not visited[y, x]:
                min_x, min_y, max_x, max_y = bfs(y, x)
                draw.rectangle([min_x, min_y, max_x, max_y], outline="red", width=2)

    highlight_img.save(highlight_path)

    return {
        "width": w,
        "height": h,
        "mismatchedPixels": mismatched,
        "mismatchPercentage": round(mismatch_pct, 4),
    }


def html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


#def render_report(meta, results):
    rows = []
    for r in results:
        sev_class = "warn" if r["mismatchPercentage"] > 0.5 else "ok"
        rows.append(f"""
        <div class="card">
          <div class="tags">
            <span class="tag">Path: <strong>{html_escape(r['path'])}</strong></span>
            <span class="tag">Viewport: {r['viewport']}</span>
            <span class="tag">Size: {r['width']}×{r['height']}</span>
            <span class="tag {sev_class}">Diff: {r['mismatchPercentage']}% ({r['mismatchedPixels']} px)</span>
          </div>
          <div class="row">
            <div>
              <h3>Base</h3>
              <img loading="lazy" src="{html_escape(r['base'])}">
            </div>
            <div>
              <h3>Compare</h3>
              <img loading="lazy" src="{html_escape(r['compare'])}">
            </div>
          </div>
        </div>
        """)

    css = """
      :root { color-scheme: light dark; }
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 16px; }
      h1, h2, h3 { margin: 0.2rem 0; }
      .meta { margin-bottom: 16px; color: #666; font-size: 0.95rem; }
      .grid { display: grid; grid-template-columns: 1fr; gap: 24px; }
      .card { border: 1px solid #ddd; border-radius: 8px; padding: 12px; overflow: hidden; }
      .row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
      .row img { width: 100%; height: auto; border: 1px solid #ccc; border-radius: 4px; }
      .tags { display: flex; gap: 8px; flex-wrap: wrap; margin: 4px 0 8px; }
      .tag { background: #f1f5f9; border: 1px solid #e2e8f0; color: #334155; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
      .footer { margin-top: 24px; color: #777; font-size: 0.9rem; }
      .warn { color: #b45309; }
      .ok { color: #065f46; }
      img {{ max-height: 80vh; object-fit: contain; }}
    """

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Visual Comparison Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
</head>
<body>
  <h1>Visual Comparison Report</h1>
  <div class="meta">
    <div>Generated: {html_escape(meta['generatedAt'])}</div>
    <div>Base: {html_escape(meta['base'])}</div>
    <div>Compare: {html_escape(meta['compare'])}</div>
    <div>Viewports: {", ".join(html_escape(v) for v in meta['viewports'])}</div>
    <div>Options: FullPage={meta['options']['fullPage']}, Headless={meta['options']['headless']}, Timezone={html_escape(meta['options']['timezone'])}, Locale={html_escape(meta['options']['locale'])}, FreezeTime={html_escape(str(meta['options']['freezeTime']))}, Hide={", ".join(html_escape(s) for s in meta['options']['hide'])}</div>
    <div>Total cases: {len(results)}</div>
  </div>
  <div class="grid">
    {"".join(rows)}
  </div>
  <div class="footer">
    Tip: Use EXCLUDE_PATTERNS and HIDE_SELECTORS to reduce false positives.
  </div>
</body>
</html>"""
    return html

def render_report(meta, results):
    def html_escape(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))

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
        <div class="card">
          <div class="tags">
            <span class="tag">Path: <strong>{html_escape(r['path'])}</strong></span>
            <span class="tag">Viewport: {r['viewport']}</span>
            <span class="tag">Size: {r['width']}×{r['height']}</span>
            <span class="tag">Diff: {r.get('mismatchPercentage', 0)}% ({r.get('mismatchedPixels', 0)} px)</span>
          </div>
          <div class="row">
            <div>
              <h3 style="padding: 4px 8px;">Base</h3>
              <img loading="lazy" src="{html_escape(r['base'])}">
            </div>
            <div>
              <h3 style="padding: 4px 8px;">Compare</h3>
              <img loading="lazy" src="{html_escape(r['compare'])}">
            </div>
          </div>
        </div>
        """)

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Visual Comparison Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
</head>
<body>
  <h1 style="margin:12px;">Visual Comparison Report</h1>
  <div class="meta">
    <div>Generated: {html_escape(meta['generatedAt'])}</div>
    <div>Base: {html_escape(meta['base'])}</div>
    <div>Compare: {html_escape(meta['compare'])}</div>
    <div>Viewports: {", ".join(html_escape(v) for v in meta['viewports'])}</div>
  </div>
  <div class="grid">
    {"".join(rows)}
  </div>
  <div class="footer">
    Tip: Hide dynamic overlays with selectors (cookie banners, chat). Use Edge/Chrome channels to avoid extra downloads in corporate networks.
  </div>
</body>
</html>"""
    return html

async def crawl_paths(base_url: str, start_paths: list[str], max_pages: int, max_depth: int,
                      keep_query: bool, exclude_patterns: list[str]) -> list[str]:
    """
    Use Playwright to crawl the base site (so JS-rendered links are included).
    Returns a sorted list of unique paths discovered.
    """
    discovered: set[str] = set()
    origin = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(base_url))

    async with async_playwright() as p:
        launch_kwargs = {"headless": True}
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
        },
    }

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
        print("No paths discovered. Check your BASE_URL and EXCLUDE_PATTERNS.")
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
                # sanitize filename
                fname_safe = (
                    path.replace("/", "_")
                        .replace("?", "__q__")
                        .replace("&", "__and__")
                        .strip("_") or "home"
                )

                base_url = urljoin(BASE_URL, path)
                compare_url = urljoin(COMPARE_URL, path)

                base_out = os.path.join(IMAGES_DIR, f"{fname_safe}_{vp_slug}_base.png")
                compare_out = os.path.join(IMAGES_DIR, f"{fname_safe}_{vp_slug}_compare.png")
                diff_out = os.path.join(IMAGES_DIR, f"{fname_safe}_{vp_slug}_diff.png")

                print(f"[{vp_slug}] {path}")
                print("  BASE    ", base_url)
                print("  COMPARE ", compare_url)

                try:
                    base_buf = await take_screenshot(page_base, base_url, FULL_PAGE, WAIT_TIME, HIDE_SELECTORS)
                    with open(base_out, "wb") as f: f.write(base_buf)
                except PWError as e:
                    print("  ! Base screenshot failed:", e)
                    continue

                try:
                    cmp_buf = await take_screenshot(page_compare, compare_url, FULL_PAGE, WAIT_TIME, HIDE_SELECTORS)
                    with open(compare_out, "wb") as f: f.write(cmp_buf)
                except PWError as e:
                    print("  ! Compare screenshot failed:", e)
                    continue

                highlight_out = os.path.join(IMAGES_DIR, f"{fname_safe}_{vp_slug}_highlight.png")
                metrics = diff_and_metrics(base_out, compare_out, diff_out, highlight_out)

                results.append({
                    "path": path,
                    "viewport": vp_slug,
                    "base": os.path.relpath(base_out, OUT_DIR).replace("\\", "/"),
                    "compare": os.path.relpath(compare_out, OUT_DIR).replace("\\", "/"),
                    "diff": os.path.relpath(diff_out, OUT_DIR).replace("\\", "/"),
        "highlight": os.path.relpath(highlight_out, OUT_DIR).replace("\\", "/"),
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