# site_visual_compare_static.py
# Visual compare between BASE_URL and COMPARE_URL for a fixed list of PATHS (no crawler).
# Takes screenshots per viewport, computes mismatch %, and generates an HTML report.

import os
import html
import asyncio
from datetime import datetime
from urllib.parse import urljoin
import numpy as np
from PIL import Image, ImageChops
from playwright.async_api import async_playwright, Error as PWError

# ------------------------------ CONFIG ---------------------------------------
BASE_URL = "https://www.comirnaty.cz/vakcina-comirnaty"  # <- change me
COMPARE_URL = "https://16700102.livepreview.pfizer/vakcina-comirnaty"  # <- change me

# Provide explicit paths to check (no auto-crawl)
PATHS = [
    "/",              # home
    # "/about",
    # "/contact",
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


# ------------------------------ HELPERS --------------------------------------
def ensure_dirs():
    os.makedirs(IMAGES_DIR, exist_ok=True)

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

# --- Lazy images helpers ---
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


def pad_to_same_size(img1: Image.Image, img2: Image.Image):
    if img1.size == img2.size:
        return img1, img2
    w = max(img1.width, img2.width)
    h = max(img1.height, img2.height)
    bg = (255, 255, 255)
    n1 = Image.new("RGB", (w, h)); n1.paste(img1, (0, 0))
    n2 = Image.new("RGB", (w, h)); n2.paste(img2, (0, 0))
    return n1, n2

# --- NEW: compute mismatch metrics in-memory (no files) ---
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

# --- robust filename sanitizer (simple) ---
import re, unicodedata

def filename_from_path(path: str) -> str:
    """
    Create a filesystem-safe ASCII filename from a URL path (no directories).
    Keeps '.', '_', '-', and alphanumerics. Converts others to underscores.
    """
    stem = path.strip("/").replace("/", "_") or "home"
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = stem.strip("_") or "home"
    return stem

# ------------------------------ REPORT ---------------------------------------

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

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Visual Comparison Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
</head>
<body>
  <h1 style=\"margin:12px;\">Visual Comparison Report</h1>
  <div class=\"meta\"> 
    <div>Generated: {html_escape(meta['generatedAt'])}</div>
    <div>Base: {html_escape(meta['base'])}</div>
    <div>Compare: {html_escape(meta['compare'])}</div>
    <div>Viewports: {", ".join(html_escape(v) for v in meta['viewports'])}</div>
    <div>Paths: {len(meta['paths'])} page(s)</div>
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

# ------------------------------ MAIN -----------------------------------------

async def run_compare():
    ensure_dirs()
    meta = {
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "base": BASE_URL,
        "compare": COMPARE_URL,
        "viewports": [f"{w}x{h}" for (w, h) in VIEWPORTS],
        "paths": PATHS,
        "options": {
            "fullPage": FULL_PAGE,
            "headless": HEADLESS,
            "timezone": TIMEZONE_ID,
            "locale": LOCALE,
            "freezeTime": FREEZE_TIME_ISO,
            "hide": HIDE_SELECTORS,
            "browserChannel": BROWSER_CHANNEL or "playwright-bundled",
        },
    }

    if not PATHS:
        print("No PATHS provided. Populate PATHS[] in the config section.")
        return

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

            vp_slug = f"{width}x{height}"

            for path in PATHS:
                fname_safe = filename_from_path(path)
                base_url = urljoin(BASE_URL, path)
                compare_url = urljoin(COMPARE_URL, path)
                base_out = os.path.join(IMAGES_DIR, f"{fname_safe}_{vp_slug}_base.png")
                compare_out = os.path.join(IMAGES_DIR, f"{fname_safe}_{vp_slug}_compare.png")

                print(f"[{vp_slug}] {path}")
                print(" BASE ", base_url)
                print(" COMPARE ", compare_url)

                try:
                    base_buf = await take_screenshot(page_base, base_url, FULL_PAGE, WAIT_TIME, HIDE_SELECTORS)
                    with open(base_out, "wb") as f:
                        f.write(base_buf)
                except PWError as e:
                    print(" ! Base screenshot failed:", e)
                    continue

                try:
                    cmp_buf = await take_screenshot(page_compare, compare_url, FULL_PAGE, WAIT_TIME, HIDE_SELECTORS)
                    with open(compare_out, "wb") as f:
                        f.write(cmp_buf)
                except PWError as e:
                    print(" ! Compare screenshot failed:", e)
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
