"""Capture real app screens using fictional data; no credentials or provider calls.

Install .[docs-screenshots], then: python scripts/capture_docs.py
Uses installed Google Chrome; pass --browser chromium for Playwright Chromium.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn
from playwright.sync_api import sync_playwright
from pypdf import PdfWriter

from meta_coder import extractors, web
from meta_coder.app_settings import AppSettings, save_app_settings
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project, write_manual
from meta_coder.settings import save_project_extractor_id


def capture(browser_channel="chrome"):
    output = ROOT / "docs/assets/screenshots"
    output.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="meta-coder-docs-"))
        stack.enter_context(patch.dict(os.environ, {"META_CODER_HOME": directory}))
        stack.enter_context(patch.object(web.credentials, "keyring_available", return_value=False))
        # Fictional extractors. One is marked as having a key, but no key is ever saved or read.
        gemini = extractors.add_extractor(extractors.Extractor(
            id="guide-gemini", name="Gemini (lab key)", provider="gemini", model="gemini-3.8-flash", parallel_requests=2, key_saved=True))
        extractors.add_extractor(extractors.Extractor(
            id="guide-local", name="Local server", provider="openai_compatible", model="qwen3.8-27b",
            base_url="http://localhost:8000/v1"))
        save_app_settings(AppSettings(drafting_extractor_id=gemini.id))
        project = create_project("Interference effects · demonstration", root=Path(directory) / "projects")
        write_manual(project, parse_coding_manual('''name: Interference effects
description: Fictional project for the MetaCoder guide.
effect_definition: Difference in response time between incongruent and congruent trials, separately for each experiment.
effects:
  Sample size:
    type: number
    description: Number of participants contributing to this effect.
  Publication status:
    type: string
    description: Publication status of the study report.
    levels:
      - value: Published
        description: Published in a peer-reviewed venue.
      - value: Unpublished
        description: Thesis, preprint, or other unpublished report.
'''))
        project.coding_sheet_path.write_text('row_id,source_pdf,locator,authors,year\nstudy-01-exp-1,demo-study.pdf,"Experiment 1, Table 2",Demo and Example,2024\nstudy-01-exp-2,demo-study.pdf,"Experiment 2, Table 4",Demo and Example,2024\n')
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(project.sources_dir / "demo-study.pdf")
        save_project_extractor_id(project, gemini.id)
        # Stable metadata keeps screenshots reproducible.
        metadata = project.path / ".meta_coder/project.json"
        metadata.write_text(metadata.read_text().replace(project.created_at, "2026-01-01T00:00:00+00:00"))
        app = web.create_app(token="guide-demo", projects_root=Path(directory) / "projects")
        sock = stack.enter_context(socket.socket())
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=browser_channel if browser_channel != "chromium" else None)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1, color_scheme="light")
                page = context.new_page()
                base = f"http://127.0.0.1:{port}/guide-demo"
                screens = {"home": "/", "settings": "/settings"}
                screens.update({name: f"/projects/{project.project_id}?tab={tab}" for name, tab in [
                    ("analysis", "analysis"), ("manual", "manual"), ("coding-sheet", "coding-sheet"),
                    ("sources", "sources"), ("matching", "identify"), ("run", "run"),
                    ("results", "results"), ("projects", "manage"),
                ]})
                for name, path in screens.items():
                    page.goto(base + path, wait_until="networkidle")
                    if name not in ("home", "settings"):
                        tab = path.split("?tab=")[1]
                        page.locator(f'[data-tab-panel="{tab}"]').wait_for(state="visible")
                    page.screenshot(path=str(output / f"{name}.png"), animations="disabled")
                    print(f"Captured {name}")
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default="chrome", choices=["chrome", "chromium"])
    capture(parser.parse_args().browser)
