#!/usr/bin/env python3
"""Live browser proof for PR 7684's first-chat managed download handoff."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from playwright.async_api import Route, async_playwright
from studio_test_kit.auth import login, seed_init_script
from studio_test_kit.lifecycle import StudioInstall, launch_studio, stop_studio

REPO = "unsloth/Qwen3.5-4B-MTP-GGUF"
VARIANT = "UD-Q4_K_XL"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def read_password(home: Path, fallback: str | None) -> str:
    if fallback:
        return fallback
    for rel in ("auth/.bootstrap_password", ".bootstrap_password"):
        path = home / rel
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    raise RuntimeError("Studio bootstrap password was not available")


async def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifacts = Path(os.environ["STUDIO_ARTIFACT_DIR"]).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    browser_name = os.environ.get("STUDIO_BROWSER", "chromium")
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    install = StudioInstall(home=home, repo=Path.cwd(), branch="ci")
    launch_studio(install, port, artifacts / "studio.log", healthz_timeout_s=240)

    state = {"started": False, "cancelled": False, "load_calls": 0}
    try:
        password = read_password(home, install.bootstrap_password)
        auth = await login(base_url, "unsloth", password)
        if auth.must_change_password:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{base_url}/api/auth/change-password",
                    headers={"Authorization": f"Bearer {auth.access_token}"},
                    json={
                        "current_password": password,
                        "new_password": "Studio-CI-7684!",
                    },
                )
                response.raise_for_status()
                changed = response.json()
            auth.access_token = changed["access_token"]
            auth.refresh_token = changed.get("refresh_token", "")
            auth.must_change_password = False
        init_script = seed_init_script(auth, [], connections_enabled=False)

        async def json_reply(route: Route, body: object, status: int = 200) -> None:
            await route.fulfill(
                status=status,
                content_type="application/json",
                body=json.dumps(body),
            )

        async def handle_api(route: Route) -> None:
            request = route.request
            path = urlparse(request.url).path
            if path == "/api/hub/cached-gguf":
                await json_reply(route, {"cached": []})
            elif path == "/api/hub/cached-models":
                await json_reply(route, {"cached": []})
            elif path == "/api/studio/download-transport-capabilities":
                await json_reply(
                    route,
                    {
                        "http": {"available": True, "reason": None},
                        "xet": {"available": False, "reason": "CI fixture"},
                    },
                )
            elif path == "/api/hub/active-downloads":
                downloads = []
                if state["started"] and not state["cancelled"]:
                    downloads = [
                        {
                            "repo_id": REPO,
                            "variant": VARIANT,
                            "transport": "http",
                            "state": "running",
                            "generation": 1,
                        }
                    ]
                await json_reply(route, {"downloads": downloads})
            elif path == "/api/hub/transport-status":
                await json_reply(
                    route,
                    {"has_partial": False, "last_transport": None, "resumable": False},
                )
            elif path == "/api/inference/validate":
                await json_reply(
                    route,
                    {
                        "valid": True,
                        "message": "CI fixture",
                        "identifier": REPO,
                        "display_name": "Qwen3.5-4B-MTP-GGUF",
                        "is_gguf": True,
                        "requires_trust_remote_code": False,
                        "requires_security_review": False,
                        "requires_transformers_upgrade": False,
                    },
                )
            elif path == "/api/hub/download" and request.method == "POST":
                payload = json.loads(request.post_data or "{}")
                assert payload.get("repo_id") == REPO, payload
                assert payload.get("gguf_variant") == VARIANT, payload
                state["started"] = True
                await json_reply(
                    route,
                    {
                        "job_key": f"model:{REPO}:{VARIANT}",
                        "state": "running",
                        "accepted": True,
                        "generation": 1,
                    },
                )
            elif path == "/api/hub/download-status":
                await json_reply(
                    route,
                    {
                        "state": "cancelled" if state["cancelled"] else "running",
                        "error": None,
                        "generation": 1,
                    },
                )
            elif path == "/api/hub/gguf-download-progress":
                await json_reply(
                    route,
                    {
                        "downloaded_bytes": 524288,
                        "completed_bytes": 0,
                        "complete_on_disk": False,
                        "expected_bytes": 1048576,
                        "progress": 0.5,
                        "cache_path": None,
                    },
                )
            elif path == "/api/hub/download/cancel" and request.method == "POST":
                state["cancelled"] = True
                await json_reply(
                    route,
                    {
                        "job_key": f"model:{REPO}:{VARIANT}",
                        "state": "cancelling",
                    },
                )
            elif path == "/api/inference/load" and request.method == "POST":
                state["load_calls"] += 1
                await json_reply(
                    route,
                    {
                        "status": "loaded",
                        "model": REPO,
                        "display_name": "Qwen3.5-4B-MTP-GGUF",
                        "is_vision": False,
                        "is_lora": False,
                        "context_length": 4096,
                    },
                )
            else:
                await route.continue_()

        async with async_playwright() as playwright:
            browser_type = getattr(playwright, browser_name)
            browser = await browser_type.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            await context.add_init_script(init_script)
            await context.route("**/api/**", handle_api)
            page = await context.new_page()
            try:
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                composer = page.locator("form:has(textarea) textarea").first
                await composer.wait_for(state="visible", timeout=30_000)
                await composer.fill("Prove that the first chat uses Downloads")
                await composer.press("Enter")

                panel = page.locator(".hub-download-panel")
                await panel.wait_for(state="visible", timeout=30_000)
                await panel.get_by_text(REPO, exact=False).wait_for(timeout=10_000)
                await panel.get_by_text(VARIANT, exact=False).wait_for(timeout=10_000)
                await panel.get_by_text("Downloading 1 item", exact=True).wait_for(timeout=10_000)
                if not state["started"]:
                    raise AssertionError("browser never called POST /api/hub/download")
                print("PASS first chat starts the default transfer through Download Manager", flush=True)
                print("PASS Downloads panel exposes the default repo, exact variant, and progress", flush=True)
                await page.screenshot(path=str(artifacts / "first-chat-download-visible.png"), full_page=True)

                await panel.get_by_role("button", name="Cancel download").click()
                await panel.get_by_text("Cancelled. Partial files kept.").wait_for(timeout=30_000)
                if state["load_calls"] != 0:
                    raise AssertionError(
                        f"/api/inference/load was called {state['load_calls']} time(s) after manager cancellation"
                    )
                print("PASS cancelling from Downloads reaches the manager and prevents model activation", flush=True)
                await page.screenshot(path=str(artifacts / "first-chat-download-cancelled.png"), full_page=True)
            except Exception:
                await page.screenshot(path=str(artifacts / "first-chat-failure.png"), full_page=True)
                raise
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
    finally:
        stop_studio(install)


if __name__ == "__main__":
    asyncio.run(main())
