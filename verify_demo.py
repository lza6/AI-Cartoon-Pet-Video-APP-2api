#!/usr/bin/env python3
"""Browser E2E for the demo page (Playwright).

    python verify_demo.py --url http://127.0.0.1:18000 --out static/demo.png

Clicks through 文生图 / 文生视频 and asserts a media element actually renders.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18000")
    ap.add_argument("--out", default="static/demo.png")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(base + "/demo", wait_until="networkidle")
        assert "2API Demo" in page.title(), page.title()

        # health badge turns green
        page.wait_for_selector("#healthTag.tag.ok", timeout=10000)

        # ---- text -> image
        page.click('#tabs button[data-mode="t2i"]')
        page.fill("#prompt", "A cozy cartoon cat in a coffee shop")
        page.click("#go")
        page.wait_for_selector("#out img", timeout=60000)
        img_src = page.get_attribute("#out img", "src")
        print("t2i ->", img_src)
        page.screenshot(path=str(out))
        assert img_src, "no image url"

        # ---- text -> video
        page.click('#tabs button[data-mode="t2v"]')
        page.click("#go")
        page.wait_for_selector("#out video", timeout=60000)
        vid_src = page.get_attribute("#out video", "src")
        print("t2v ->", vid_src)
        assert vid_src, "no video url"

        # ---- account pool rendered
        page.click("#quota")
        page.wait_for_timeout(600)
        pool_text = page.inner_text("#pool")
        assert "t2v_prompt" in pool_text, pool_text[:200]
        print("pool rows ok")

        page.screenshot(path=str(out))
        browser.close()
    print("DEMO E2E PASS ->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
