
# Website Visual Visual-Regression Comparator (Playwright + Pillow)

Compare two websites (e.g., prod vs. preview) by taking screenshots at multiple viewports and computing pixel diffs. Includes two modes:

- **Targeted compare**: run against a fixed list of paths (`visual_compare.py`).
- **Crawler compare**: auto-crawl internal links on a base site, then compare page-by-page (`site_visual_crawler_compare.py`).

Both modes generate an **HTML report** and save PNGs under `visual_diff/`.

---

## What’s in this repo

- `visual_compare.py` – fixed-path visual comparison runner.
- `site_visual_crawler_compare.py` – crawler that discovers internal links and compares them.
- `.vscode/launch.json` – VS Code debug configuration to run `visual_compare.py`.

> Note: The report currently shows **Base** and **Compare** images side-by-side. Diff/Highlight PNGs are still generated and saved in `visual_diff/images/` for offline inspection.

---

## Features

- Headless Playwright browsing (or opt-in to use installed Edge/Chrome channels).
- Multiple **viewports** (desktop & mobile examples included).
- Optional **full-page** screenshots.
- CSS overlay to **hide dynamic UI** (cookie banners, chat widgets, ads) and disable animations.
- Optional **time freeze** to stabilize dates/times during rendering.
- **Pixel diff metrics** (mismatched pixel count & percentage). Highlight PNGs in crawler mode.
- Clean, single-file **HTML report** with lazy-loaded images.

---

## Requirements

- Python 3.10+
- Pip packages: `playwright`, `pillow`, `numpy`
- Playwright browser binaries (installed via `playwright install`)

### Install

```bash
# 1) Create and activate a virtual environment (recommended)
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

# 2) Install Python dependencies
pip install --upgrade pip
pip install playwright pillow numpy

# 3) Install Playwright browsers (Chromium by default)
python -m playwright install
# On Linux, if you need system dependencies too:
python -m playwright install --with-deps
```

> **Corporate networks**: To avoid downloading Playwright’s bundled Chromium, set `BROWSER_CHANNEL = "msedge"` or `"chrome"` in the scripts to use an already-installed browser.

---

## Configuration

Both scripts are configured via constants at the top of each file. Adjust as needed before running.

### `visual_compare.py` (fixed list of paths)

Key options:

- `BASE_URL` / `COMPARE_URL`: The two environments to compare.
- `PATHS`: List of routes to visit (e.g., `/`, `/products`, `/contact`).
- `VIEWPORTS`: List of `(width, height)` tuples.
- `FULL_PAGE`: Capture full-page screenshots when `True`.
- `WAIT_TIME`: Seconds to wait after page load for UI to stabilize.
- `HIDE_SELECTORS`: CSS selectors to hide (cookie banners, chat widgets, ads, ARIA-live regions, etc.).
- `BROWSER_CHANNEL`: `None`, `"msedge"`, or `"chrome"`.
- `HEADLESS`, `TIMEZONE_ID`, `LOCALE`, `FREEZE_TIME_ISO`.
- `OUT_DIR`: Output directory (default `visual_diff/`).

### `site_visual_crawler_compare.py` (auto-crawl)

Adds crawling controls on top of the options above:

- `START_PATHS`: Seed paths to start the crawl (default `['/']`).
- `MAX_PAGES`: Safety cap for total pages to compare (default `50`).
- `MAX_DEPTH`: BFS depth from each seed (default `2`).
- `KEEP_QUERY`: When `True`, treats `?a=1` and `?a=2` as different pages.
- `EXCLUDE_PATTERNS`: Regex list to skip carts, checkouts, login/admin, and static assets.
- Generates **Highlight** PNGs that draw red rectangles around contiguous diff regions.

### Hiding dynamic UI & freezing time

- **Hiding UI**: Add selectors (e.g., `.cookie-banner`, `.chat-widget`, `[aria-live='polite']`) to `HIDE_SELECTORS` to reduce noise.
- **Freeze time**: Set `FREEZE_TIME_ISO` (e.g., `"2025-01-01T00:00:00Z"`) to fix `Date.now()` and related timers for more stable renders.

---

## Running

From the project root (after configuring values inside the scripts):

### Fixed-path compare

```bash
python visual_compare.py
```

### Crawler compare

```bash
python site_visual_crawler_compare.py
```

### VS Code

A debug configuration is provided. Open the repo in VS Code, then run **Python: visual_compare** from the **Run and Debug** panel. You can duplicate that config for the crawler script if desired.

---

## Output

- HTML report at:
  - `visual_diff/report.html`
- Images under:
  - `visual_diff/images/`
    - `*_base.png` – screenshot from `BASE_URL`
    - `*_compare.png` – screenshot from `COMPARE_URL`
    - `*_diff.png` – pixel-diff image (always created)
    - `*_highlight.png` – **crawler mode only**, rectangles around diff clusters

> Filenames include a slugified path and viewport, e.g. `home_1366x768_base.png`.

### Sample folder tree

```
visual_diff/
├─ images/
│  ├─ home_1366x768_base.png
│  ├─ home_1366x768_compare.png
│  ├─ home_1366x768_diff.png
│  └─ ...
└─ report.html
```

---

## Tips & Troubleshooting

- **False positives**: Add more selectors to `HIDE_SELECTORS` and increase `WAIT_TIME` slightly.
- **Auth-protected pages**: These scripts don’t include auth flows. You can extend them to set cookies or use Playwright’s storage state.
- **Blocked downloads**: Use `BROWSER_CHANNEL = "msedge"` or `"chrome"` to use a locally installed browser.
- **Fonts**: The scripts wait for `document.fonts.ready` when available; ensure required web fonts are accessible.
- **Long pages**: If `FULL_PAGE=True` is slow or memory-heavy, set it to `False` or scope your comparison to critical sections.

---

## .gitignore suggestion

If you don’t want to commit large outputs:

```
/visual_diff/
```

---

## License

Add your preferred license (e.g., MIT) here.

---

## Acknowledgments

- Built with [Microsoft Playwright for Python](https://playwright.dev/python/) and [Pillow](https://python-pillow.org/).

