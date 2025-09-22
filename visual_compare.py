import os
import asyncio
from datetime import datetime
from PIL import Image, ImageChops
import numpy as np
from playwright.async_api import async_playwright

# ---------------- Configuration ----------------
BASE_URL = "https://comirnatycz.livepreview.pfizer/"
COMPARE_URL = "https://16700102.livepreview.pfizer/"
PATHS = ["/", "/products", "/contact"]
VIEWPORTS = [(1366, 768), (390, 844)]
OUT_DIR = "visual_diff"
FULL_PAGE = True
WAIT_TIME = 0.3  # seconds to allow UI to settle
HIDE_SELECTORS = [".cookie-banner", ".chat-widget", ".ad", "[aria-live='polite']"]

# Use installed browsers instead of downloads by setting this to "msedge" or "chrome"
# Leave as None to use Playwright's downloaded Chromium.
BROWSER_CHANNEL = None  # e.g., "msedge" or "chrome"
HEADLESS = True
TIMEZONE_ID = "UTC"
LOCALE = "en-US"
FREEZE_TIME_ISO = None  # e.g., "2025-01-01T00:00:00Z"
# ------------------------------------------------

os.makedirs(OUT_DIR, exist_ok=True)
IMAGES_DIR = os.path.join(OUT_DIR, "images")
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

async def take_screenshot(page, url: str, full_page: bool, wait_time: float, hide_selectors):
    await page.goto(url, wait_until="networkidle", timeout=60_000)
    try:
        await page.evaluate("(document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve()")
    except:
        pass
    await page.add_style_tag(content=overlay_css(hide_selectors))
    if wait_time and wait_time > 0:
        await asyncio.sleep(wait_time)
    await page.evaluate("window.scrollTo(0, 0)")
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

def diff_and_metrics(base_path, compare_path, diff_path):
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

    return {
        "width": diff_arr.shape[1],
        "height": diff_arr.shape[0],
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
            <div>
              <h3>Diff</h3>
              <img loading="lazy" src="{html_escape(r['diff'])}">
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
  </div>
  <div class="grid">
    {"".join(rows)}
  </div>
  <div class="footer">
    Tip: Use hide selectors for dynamic overlays (cookie banners, chat widgets, ads).
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

async def main():
    results = []
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
        },
    }

    async with async_playwright() as p:
        launch_kwargs = {"headless": HEADLESS}
        if BROWSER_CHANNEL:
            launch_kwargs["channel"] = BROWSER_CHANNEL

        browser = await p.chromium.launch(**launch_kwargs)

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

            for pth in PATHS:
                vp_slug = f"{width}x{height}"
                path_slug = (pth.strip("/").replace("/", "_") or "home")
                base_url = f"{BASE_URL}{pth}"
                compare_url = f"{COMPARE_URL}{pth}"

                base_out = os.path.join(IMAGES_DIR, f"{path_slug}_{vp_slug}_base.png")
                compare_out = os.path.join(IMAGES_DIR, f"{path_slug}_{vp_slug}_compare.png")
                diff_out = os.path.join(IMAGES_DIR, f"{path_slug}_{vp_slug}_diff.png")

                base_buf = await take_screenshot(page_base, base_url, FULL_PAGE, WAIT_TIME, HIDE_SELECTORS)
                with open(base_out, "wb") as f:
                    f.write(base_buf)

                cmp_buf = await take_screenshot(page_compare, compare_url, FULL_PAGE, WAIT_TIME, HIDE_SELECTORS)
                with open(compare_out, "wb") as f:
                    f.write(cmp_buf)

                metrics = diff_and_metrics(base_out, compare_out, diff_out)

                results.append({
                    "path": pth,
                    "viewport": vp_slug,
                    "base": os.path.relpath(base_out, OUT_DIR).replace("\\", "/"),
                    "compare": os.path.relpath(compare_out, OUT_DIR).replace("\\", "/"),
                    "diff": os.path.relpath(diff_out, OUT_DIR).replace("\\", "/"),
                    **metrics,
                })

            await ctx_base.close()
            await ctx_compare.close()

        await browser.close()

    report_html = render_report(meta, results)
    report_path = os.path.join(OUT_DIR, "report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    print(f"✅ Done! Open: {report_path}")

if __name__ == "__main__":
    asyncio.run(main())
