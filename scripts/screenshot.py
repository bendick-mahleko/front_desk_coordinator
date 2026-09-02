"""Screenshot a running Streamlit page — `uv run screenshot`.

Written after fixing the same UI defect three times without ever seeing it. Each
fix was reasoned from the stylesheet, verified by computed contrast ratios, and
wrong in a way only a picture would have shown.

Uses the Chrome already on the machine (`channel="chrome"`) rather than
downloading a browser, and waits for Streamlit's *app view* rather than for page
load — a plain screenshot catches the loading skeleton, which is exactly what
happened on the first attempt.

    uv run screenshot                          # both apps, if they are running
    uv run screenshot --url http://... --out x.png
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

DEFAULT_TARGETS: tuple[tuple[str, str], ...] = (
    ("http://127.0.0.1:8501", "patient"),
    ("http://127.0.0.1:8502", "clinical"),
)

# Streamlit renders a skeleton first and the real view once the websocket has
# delivered a script run. This is the marker for the second.
APP_VIEW = '[data-testid="stAppViewContainer"]'
SKELETON = '[data-testid="stSkeleton"]'


def capture(url: str, out: Path, *, width: int, height: int, full: bool) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        # channel="chrome" uses the installed browser, so nothing is downloaded.
        browser = play.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector(APP_VIEW, timeout=30_000)
            # The skeleton and the app view coexist briefly; wait it out.
            with contextlib.suppress(PlaywrightTimeout):
                page.wait_for_selector(SKELETON, state="detached", timeout=15_000)
            # Streamlit fades content in after the skeleton detaches, and a
            # 1.5s wait caught a half-painted frame — a screenshot showing a
            # title and nothing else, which sent me looking for a bug in the app
            # rather than in the tool. Wait for the sidebar to have painted, then
            # settle.
            with contextlib.suppress(PlaywrightTimeout):
                page.wait_for_selector('[data-testid="stSidebar"]', timeout=15_000)
            page.wait_for_timeout(5_000)
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=full)
            return True
        except PlaywrightTimeout:
            print(f"  {url}: never finished rendering", file=sys.stderr)
            return False
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screenshot a running Streamlit page.")
    parser.add_argument("--url", help="a single page to capture")
    parser.add_argument("--out", help="where to write it")
    parser.add_argument("--width", type=int, default=1500)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument(
        "--full", action="store_true", help="the whole scrollable page, not just the viewport"
    )
    parser.add_argument("--dir", default="screenshots", help="output directory for the default set")
    args = parser.parse_args(argv)

    if args.url:
        target = Path(args.out or "screenshot.png")
        ok = capture(args.url, target, width=args.width, height=args.height, full=args.full)
        return 0 if ok else 1

    captured = 0
    for url, name in DEFAULT_TARGETS:
        out = Path(args.dir) / f"{name}.png"
        print(f"{name}: {url}")
        if capture(url, out, width=args.width, height=args.height, full=args.full):
            print(f"  -> {out}")
            captured += 1
    if not captured:
        print("Nothing captured. Are the apps running? See docs/runbook.md.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
