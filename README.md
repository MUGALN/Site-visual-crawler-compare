
# Site Visual Compare (Playwright + Python)

A small, script-first toolkit to **visually compare a Base site vs. a Compare site** by taking screenshots and reporting pixel differences. It supports two ways of picking pages:

- **Static list** – compare a fixed list of paths (`site_visual_compare_static.py`).
- **Crawler** – auto-crawl internal links on the Base site, then compare the same paths on the Compare site (`site_visual_crawler_compare.py`).

Both modes can run across multiple viewports and generate a simple, self-contained **HTML report** under `visual_diff/report.html`.

---

## Contents

```
site_visual_compare_static.py     # Static list visual compare (Base vs Compare)
site_visual_crawler_compare.py    # Auto-crawler visual compare (Base vs Compare)
.vscode/launch.json               # VS Code debug config (update program paths)
```

> The provided `launch.json` may point to `visual_compare.py`. Update `program` to one of the scripts above (see examples below).

---

## Requirements

- Python **3.9+**
- Packages: `playwright`, `pillow`, `numpy`
- Playwright browsers: run `python -m playwright install` at least once (or set `BROWSER_CHANNEL` to use your locally installed Edge/Chrome).

### Install

```bash
# Create and activate a virtual environment
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows (PowerShell)
# .venv\Scripts\Activate.ps1

# Install dependencies
pip install --upgrade pip
pip install playwright pillow numpy

# Install Playwright browsers
python -m playwright install
```

---

## Quick Start

### Option A: Static list compare

1) Open **`site_visual_compare_static.py`** and set:

- `BASE_URL` – reference site (e.g., production)
- `COMPARE_URL` – candidate site (e.g., staging/preview)
- `PATHS` – list of URL paths to check, e.g. `["/", "/about"]`

2) Run:

```bash
python site_visual_compare_static.py
```

**Outputs**
- Per-viewport screenshots under `visual_diff/images/`:
  - `{{path_slug}}_{{WIDTHxHEIGHT}}_base.png`
  - `{{path_slug}}_{{WIDTHxHEIGHT}}_compare.png`
- HTML summary: `visual_diff/report.html` (shows Base and Compare side-by-side with mismatch metrics).

> Static mode includes quality-of-life tweaks for lazy images: it forces `<img loading="lazy">` to eager, progressively auto-scrolls to trigger lazy loaders, waits for images to complete, and then captures.

---

### Option B: Crawler compare

1) Open **`site_visual_crawler_compare.py`** and set:

- `BASE_URL`, `COMPARE_URL`
- `START_PATHS` – crawl seeds (default `["/"]`)
- `MAX_PAGES`, `MAX_DEPTH` – caps for breadth-first crawl
- Optional: `KEEP_QUERY` (treat different query strings as separate pages), `EXCLUDE_PATTERNS` (regex paths to skip)

2) Run:

```bash
python site_visual_crawler_compare.py
```

**Outputs**
- Per-viewport screenshots under `visual_diff/images/`:
  - `..._base.png`, `..._compare.png`, plus `..._diff.png` and `..._highlight.png` (highlight boxes around change clusters)
- HTML summary: `visual_diff/report.html` (currently shows Base & Compare panes; diff/highlight are saved to disk for manual inspection)

> Note: The crawler doesn’t currently auto-scroll to trigger lazy loaders before capture. If you rely on heavy lazy loading, you may prefer static mode or extend the crawler with similar lazy-image helpers.

---

## Configuration (shared)

Both scripts expose these common options at the top:

- **`VIEWPORTS`**: list of `(width, height)` pairs, e.g. `[(1366, 768), (390, 844)]`.
- **`FULL_PAGE`**: capture full page (`True`/`False`).
- **`HEADLESS`**: run browser headless; set `False` to watch runs.
- **`BROWSER_CHANNEL`**: `None` (Playwright’s bundled Chromium) or `"msedge"` / `"chrome"` to use an installed browser.
- **`TIMEZONE_ID`, `LOCALE`**: emulate time zone and locale.
- **`FREEZE_TIME_ISO`**: freeze time to a fixed instant (e.g., `"2025-01-01T00:00:00Z"`) for reproducible renders.
- **`HIDE_SELECTORS`**: selectors hidden via injected CSS (useful for cookie banners, chat widgets, ads, etc.).
- **`OUT_DIR`**: default `visual_diff`; images are under `visual_diff/images`.

### Static-only helpers
- **`IMAGE_WAIT_MS`**: max wait for images to complete.
- **`SCROLL_STEP_PX`, `SCROLL_PAUSE_MS`**: progressive auto-scroll tuning to trigger lazy loaders.

### Crawler-only controls
- **`START_PATHS`**, **`MAX_PAGES`**, **`MAX_DEPTH`**: BFS crawl parameters.
- **`KEEP_QUERY`**: include query strings in uniqueness if `True`.
- **`EXCLUDE_PATTERNS`**: regex patterns to skip (e.g., `/admin`, `/login`, file extensions like `.(pdf|zip|jpg|png|svg|webp|ico)`).

---

## How it works (high level)

1. Navigate and wait for network to settle (`wait_until="networkidle"`), then attempt to wait for web fonts.
2. Inject CSS that disables animations/transitions and hides configured selectors to reduce noise.
3. *(Static only)* Force images to eager, progressively auto-scroll, and wait for images to be complete.
4. Capture screenshots per viewport (scrolls to top before capture) with animations disabled.
5. Compute pixel mismatch metrics (count and %). Crawler additionally saves a raw diff and a highlight image.
6. Generate `visual_diff/report.html` summarizing the run with per-page, per-viewport tags.

---

## VS Code: launch configurations

Update `.vscode/launch.json` to point at the script you want to run. Example:

```jsonc
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: Static Compare",
      "type": "python",
      "request": "launch",
      "program": "${workspaceFolder}/site_visual_compare_static.py",
      "console": "integratedTerminal",
      "justMyCode": true,
      "env": { "PYTHONASYNCIODEBUG": "0" }
    },
    {
      "name": "Python: Crawler Compare",
      "type": "python",
      "request": "launch",
      "program": "${workspaceFolder}/site_visual_crawler_compare.py",
      "console": "integratedTerminal",
      "justMyCode": true,
      "env": { "PYTHONASYNCIODEBUG": "0" }
    }
  ]
}
```

---

## Tips & troubleshooting

- **Reduce false positives**: add cookie/chat/ads selectors to `HIDE_SELECTORS`.
- **Corporate networks**: set `BROWSER_CHANNEL = "msedge"` or `"chrome"` to reuse locally installed browsers (avoids downloading the Playwright bundle).
- **Determinism**: set `FREEZE_TIME_ISO`, `TIMEZONE_ID`, and `LOCALE`.
- **Long pages**: prefer `FULL_PAGE=True` and ensure enough viewport height/scroll to render content prior to capture.
- **Timeouts**: raise Playwright navigation timeout or stabilize content with a small `WAIT_TIME`.

---

## Roadmap (ideas)

- Show `diff` / `highlight` panes directly in the crawler HTML report.
- Thresholding / perceptual diffs to tolerate minor noise.
- Parallelize per-viewport or per-path runs.
- CLI flags (argparse) to avoid editing constants in the file.

---

## License

Choose a license (e.g., MIT, Apache-2.0) and add it here.

---

## Acknowledgements

- [Playwright](https://playwright.dev/) for browser automation.
- [Pillow](https://python-pillow.org/) and [NumPy](https://numpy.org/) for image processing and array ops.

