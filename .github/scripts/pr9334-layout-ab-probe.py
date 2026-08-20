#!/usr/bin/env python3
"""Secret-free live Studio assertion for PR 9334 A/B branches."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import httpx
from studio_test_kit.auth import login, seed_init_script
from studio_test_kit.lifecycle import StudioInstall, launch_studio, stop_studio
from studio_test_kit.ui import open_chat

REPO_ID = "unsloth/PR-9334-layout-fixture"
EXPECTED_BYTES = 512 * 1024 * 1024
DOWNLOADED_BYTES = 64 * 1024 * 1024
LATEST = "2099.9.3"
UPDATE_STATUS = {
    "current_version": "2026.8.7",
    "latest_version": LATEST,
    "update_available": True,
    "install_source": "pypi",
    "can_show_web_notification": True,
    "release_notes_url": "https://unsloth.ai/docs/new/changelog",
    "checked_at": "2099-01-01T00:00:00Z",
    "reason": None,
    "error": None,
}
RELEASE_NOTES = {
    "version": LATEST,
    "markdown": "\n".join([
        f"## {LATEST}", "", "### What's Changed", "",
        "- Faster local inference and a long enough line to exercise the real card layout.",
        "- Studio download and update reliability improvements.",
    ]),
    "matched": True,
    "truncated": False,
    "source": "ci-layout-fixture",
    "release_notes_url": "https://unsloth.ai/docs/new/changelog",
    "error": None,
}
LLAMA_STATUS = {
    "supported": True,
    "update_available": False,
    "llama_update_available": False,
    "update_component": None,
    "installed_tag": "b10333",
    "latest_tag": "b10333",
    "update_size_bytes": None,
    "component": "llama.cpp",
    "job": {"state": "idle", "message": "", "progress": None, "error": None},
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fixture_script() -> str:
    fixtures = {
        "active": {"downloads": [{
            "repo_id": REPO_ID,
            "variant": None,
            "transport": "http",
            "cancel_transport": None,
            "state": "running",
            "generation": 1,
        }]},
        "progress": {
            "downloaded_bytes": DOWNLOADED_BYTES,
            "completed_bytes": DOWNLOADED_BYTES,
            "complete_on_disk": False,
            "expected_bytes": EXPECTED_BYTES,
            "progress": DOWNLOADED_BYTES / EXPECTED_BYTES,
            "cache_path": "/ci/pr-9334",
            "target_present": False,
            "cache_measured": True,
        },
        "update": UPDATE_STATUS,
        "notes": RELEASE_NOTES,
        "llama": LLAMA_STATUS,
    }
    return f"""
(() => {{
  const fixtures = {json.dumps(fixtures)};
  const realFetch = window.fetch.bind(window);
  window.__pr9334Requests = {{active: 0, status: 0, progress: 0, update: 0}};
  window.__unslothE2EWebUpdateDelayMs = 100;
  for (const key of Object.keys(localStorage)) {{
    if (key.startsWith("unsloth_web_update_dismissed")) localStorage.removeItem(key);
  }}
  const answer = (payload) => new Response(JSON.stringify(payload), {{
    status: 200, headers: {{"content-type": "application/json"}},
  }});
  window.fetch = async (input, init) => {{
    const raw = typeof input === "string" ? input
      : input instanceof Request ? input.url : String(input);
    const url = new URL(raw, window.location.origin);
    if (url.pathname === "/api/hub/active-downloads") {{
      window.__pr9334Requests.active += 1; return answer(fixtures.active);
    }}
    if (url.pathname === "/api/hub/datasets/active-downloads") return answer({{downloads: []}});
    if (url.pathname === "/api/hub/download-status") {{
      window.__pr9334Requests.status += 1;
      return answer({{state: "running", error: null, generation: 1}});
    }}
    if (url.pathname === "/api/hub/download-progress"
        || url.pathname === "/api/hub/gguf-download-progress") {{
      window.__pr9334Requests.progress += 1; return answer(fixtures.progress);
    }}
    if (url.pathname === "/api/studio/update-status") {{
      window.__pr9334Requests.update += 1; return answer(fixtures.update);
    }}
    if (url.pathname === "/api/studio/release-notes") return answer(fixtures.notes);
    if (url.pathname === "/api/llama/update-status") return answer(fixtures.llama);
    return realFetch(input, init);
  }};
}})();
"""


MEASURE = r"""
() => {
  const panel = document.querySelector('.hub-download-panel');
  const banner = document.querySelector('[data-testid="web-update-banner"]');
  if (!panel || !banner) return null;
  const wrapper = panel.parentElement;
  const rail = wrapper && wrapper.parentElement;
  if (!wrapper || !rail || banner.parentElement !== rail) return null;
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            width: r.width, height: r.height};
  };
  const clippedHeight = (el, clipper) => {
    const a = el.getBoundingClientRect(), b = clipper.getBoundingClientRect();
    return Math.max(0, Math.min(a.bottom, b.bottom, innerHeight)
      - Math.max(a.top, b.top, 0));
  };
  const cancel = panel.querySelector('button[aria-label="Cancel download"]');
  const header = panel.firstElementChild;
  const row = panel.querySelector('li');
  return {
    viewport: {width: innerWidth, height: innerHeight},
    rail_rect: rect(rail),
    rail_inline_bottom: rail.style.bottom,
    rail_inline_max_height: rail.style.maxHeight,
    rail_css_max_height: getComputedStyle(rail).maxHeight,
    rail_scroll_height: rail.scrollHeight,
    rail_client_height: rail.clientHeight,
    rail_child_count: rail.childElementCount,
    banner_height: banner.getBoundingClientRect().height,
    wrapper_height: wrapper.getBoundingClientRect().height,
    panel_height: panel.getBoundingClientRect().height,
    panel_natural_height: panel.scrollHeight,
    panel_visible_height: clippedHeight(panel, rail),
    header_height: header ? header.getBoundingClientRect().height : 0,
    header_visible_height: header ? clippedHeight(header, rail) : 0,
    row_height: row ? row.getBoundingClientRect().height : 0,
    row_visible_height: row ? clippedHeight(row, rail) : 0,
    panel_text: panel.innerText.replace(/\s+/g, ' ').trim(),
    cancel_present: Boolean(cancel),
    cancel_visible: Boolean(cancel && clippedHeight(cancel, rail) > 1),
    requests: window.__pr9334Requests,
  };
}
"""


async def authenticate(base_url: str, password: str):
    auth = await login(base_url, "unsloth", password)
    # Every CI job uses a fresh home, so this login is necessarily the bootstrap
    # credential and must be rotated before authenticated application routes work.
    new_password = "PR9334-CI-Layout-Only!"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{base_url}/api/auth/change-password",
            headers={"Authorization": f"Bearer {auth.access_token}"},
            json={"current_password": password, "new_password": new_password},
        )
        response.raise_for_status()
        body = response.json()
    auth.access_token = body["access_token"]
    auth.refresh_token = body.get("refresh_token", "")
    return auth


async def run_probe(base_url: str, password: str, artifact_dir: Path) -> dict:
    auth = await authenticate(base_url, password)
    init = seed_init_script(auth, [])
    browser = os.environ.get("STUDIO_BROWSER", "chromium")
    async with open_chat(
        base_url,
        init_scripts=[init, fixture_script()],
        viewport=(1366, 640),
        headless=True,
        browser_name=browser,
    ) as sp:
        page = sp.page
        await page.goto(base_url, wait_until="domcontentloaded")
        await page.locator("form:has(textarea) textarea").first.wait_for(
            state="visible", timeout=60_000
        )
        await page.locator('[data-testid="web-update-banner"]').wait_for(
            state="attached", timeout=30_000
        )
        await page.locator('.hub-download-panel').wait_for(state="attached", timeout=30_000)
        await page.wait_for_function(
            "window.__pr9334Requests.active > 0 && window.__pr9334Requests.progress > 0",
            timeout=30_000,
        )
        previous = None
        stable = 0
        for _ in range(24):
            facts = await page.evaluate(MEASURE)
            signature = None if facts is None else (
                round(facts["rail_rect"]["top"], 2),
                round(facts["rail_rect"]["height"], 2),
                round(facts["panel_height"], 2),
                round(facts["panel_visible_height"], 2),
            )
            stable = stable + 1 if signature is not None and signature == previous else 0
            previous = signature
            if stable >= 3:
                break
            await page.wait_for_timeout(250)
        facts = await page.evaluate(MEASURE)
        if facts is None or facts["rail_child_count"] != 2:
            raise RuntimeError(f"fixture did not render exactly two cards: {facts}")
        await page.screenshot(
            path=str(artifact_dir / "pr9334-layout.png"),
            clip={"x": 420, "y": 0, "width": 946, "height": 640},
        )
        (artifact_dir / "facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
        return facts


async def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifact_dir = Path(os.environ["STUDIO_ARTIFACT_DIR"]).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    install = StudioInstall(home=home, repo=Path.cwd(), branch=os.environ.get("GITHUB_SHA", "HEAD"))
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = artifact_dir / "studio.log"
    try:
        launch_studio(install, port, log_path)
        password_path = home / "auth" / ".bootstrap_password"
        if not password_path.is_file():
            raise RuntimeError("Studio bootstrap password was not created")
        facts = await run_probe(base_url, password_path.read_text(encoding="utf-8").strip(), artifact_dir)
        visible = float(facts["panel_visible_height"])
        cancel_visible = bool(facts["cancel_visible"])
        print(
            "PR9334_FACT "
            f"panel_visible_height={visible:.2f} "
            f"panel_natural_height={float(facts['panel_natural_height']):.2f} "
            f"header_visible_height={float(facts['header_visible_height']):.2f} "
            f"cancel_visible={str(cancel_visible).lower()} "
            f"rail_bottom={facts['rail_inline_bottom']} "
            f"rail_max_height={facts['rail_inline_max_height']}",
            flush=True,
        )
        if visible < 100 or not cancel_visible:
            print(
                "FAIL PR9334 download panel remains clipped; expected >=100px and visible Cancel",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1)
        print("PASS PR9334 download panel keeps a usable header + row", flush=True)
    finally:
        stop_studio(install)


if __name__ == "__main__":
    asyncio.run(main())
