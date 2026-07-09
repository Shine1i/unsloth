#!/usr/bin/env python3
"""PR 6993 live Studio probe for Live Monitor vs Run Settings overlap."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from studio_test_kit.auth import login, seed_init_script


def pass_log(message: str) -> None:
    print(f"PASS {message}", flush=True)


def fail(message: str) -> None:
    print(f"FAIL {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_unsloth_bin(home: Path) -> Path:
    candidates = [
        home / "bin" / "unsloth",
        home / "bin" / "unsloth.exe",
        home / "unsloth_studio" / "bin" / "unsloth",
        home / "unsloth_studio" / "Scripts" / "unsloth.exe",
    ]
    candidates.extend(home.glob(".venv*/*/unsloth"))
    candidates.extend(home.glob(".venv*/Scripts/unsloth.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    fail(f"could not find unsloth CLI under {home}")


def read_bootstrap_password(home: Path, log_path: Path) -> str | None:
    for rel in ("auth/.bootstrap_password", ".bootstrap_password"):
        path = home / rel
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(
        r"(?i)(?:bootstrap|initial|generated)\s*password(?:\s+is)?\s*[:=]?\s+(\S+)",
        log_text,
    )
    return match.group(1).strip().strip(".,") if match else None


def wait_for_health(base_url: str, timeout_s: int = 180) -> str:
    deadline = time.time() + timeout_s
    paths = ("/healthz", "/api/health", "/")
    while time.time() < deadline:
        for path in paths:
            try:
                with urllib.request.urlopen(f"{base_url}{path}", timeout=3) as resp:
                    if resp.status < 500:
                        return path
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
        time.sleep(2)
    fail(f"Studio did not become healthy within {timeout_s}s")


def start_studio(home: Path, log_path: Path, port: int) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(home)
    env.pop("STUDIO_HOME", None)
    cmd = [str(find_unsloth_bin(home)), "studio", "-H", "127.0.0.1", "-p", str(port)]
    log_handle = log_path.open("w", encoding="utf-8")
    print("Launching: " + " ".join(cmd), flush=True)
    if os.name == "nt":
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
        )
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    log_handle.close()
    return proc


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        proc.terminate()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()


async def auth_init(base_url: str, password: str) -> str:
    auth = await login(base_url, "unsloth", password)
    pass_log("Studio API login succeeded")
    if auth.must_change_password:
        new_password = os.environ.get("STUDIO_TEST_PASSWORD", "UnslothStudioCI2026!")
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
        pass_log("Studio first-boot password change completed")
    return seed_init_script(
        auth,
        [],
        extra_local_storage={
            "unsloth_monitor_overlay": {
                "state": {"isOpen": True, "isMinimized": False},
                "version": 0,
            },
        },
    )


async def run_browser_probe(base_url: str, password: str, artifact_dir: Path) -> None:
    browser_name = os.environ.get("STUDIO_BROWSER", "chromium")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    system_payload = {
        "platform": "Linux",
        "python_version": "3.12",
        "device_backend": "cuda",
        "uptime_seconds": 120,
        "cpu": {
            "logical_count": 16,
            "physical_count": 8,
            "usage_percent": 12,
            "frequency_mhz": 3200,
        },
        "memory": {
            "total_gb": 64,
            "available_gb": 37.12,
            "percent_used": 42,
            "process_used_mb": 512,
        },
        "disk": {"total_gb": 512, "free_gb": 300, "percent_used": 41},
        "gpu": {
            "available": True,
            "backend": "cuda",
            "devices": [
                {
                    "index": 0,
                    "name": "CI GPU",
                    "memory_total_gb": 24,
                    "vram_used_gb": 12,
                    "vram_free_gb": 12,
                    "vram_utilization_pct": 50,
                }
            ],
        },
        "ml_packages": {},
    }
    init_script = await auth_init(base_url, password)
    async with async_playwright() as playwright:
        browser = await getattr(playwright, browser_name).launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        await context.add_init_script(init_script)
        page = await context.new_page()
        await page.route(
            "**/api/system",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(system_payload),
            ),
        )
        await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
        await page.locator("form:has(textarea) textarea").first.wait_for(
            state="visible", timeout=30_000
        )
        status_chip = page.get_by_role(
            "button", name=re.compile(r"RAM:\s*42%\s*\|\s*VRAM:\s*50%")
        ).first
        await status_chip.wait_for(state="visible", timeout=30_000)
        pass_log("inline RAM/VRAM status chip is visible in the chat header")

        await page.get_by_role("button", name=re.compile("Open run settings", re.I)).click()
        panel = page.locator('aside[data-tour="chat-settings"]').first
        await panel.wait_for(state="visible", timeout=10_000)
        monitor = page.locator(".settings-surface.fixed.bottom-4.right-4").first
        await monitor.wait_for(state="visible", timeout=30_000)
        await page.wait_for_timeout(1200)

        boxes = await page.evaluate(
            """
            () => {
              const panel = document.querySelector('aside[data-tour="chat-settings"]');
              const monitor = document.querySelector('.settings-surface.fixed.bottom-4.right-4');
              if (!panel || !monitor) return null;
              const p = panel.getBoundingClientRect();
              const m = monitor.getBoundingClientRect();
              return {
                panel: {left: p.left, right: p.right, width: p.width},
                monitor: {left: m.left, right: m.right, width: m.width},
                gap: p.left - m.right,
              };
            }
            """
        )
        if not boxes:
            fail("could not measure Run Settings panel and Live Monitor bounds")
        if boxes["gap"] < 12:
            fail(f"Live Monitor still overlaps or crowds Run Settings: {boxes}")
        screenshot = artifact_dir / f"pr6993-live-monitor-{browser_name}.png"
        await page.screenshot(path=str(screenshot), full_page=True)
        pass_log(
            "Live Monitor avoids Run Settings overlap "
            f"(gap={boxes['gap']:.1f}px, screenshot={screenshot.name})"
        )
        await context.close()
        await browser.close()


async def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifact_dir = Path(os.environ.get("STUDIO_ARTIFACT_DIR", "studio-live-artifacts")).resolve()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc: subprocess.Popen | None = None
    try:
        log_path = artifact_dir / "studio.log"
        proc = start_studio(home, log_path, port)
        health_path = wait_for_health(base_url)
        pass_log(f"Studio healthy at {health_path} on {base_url}")
        password = read_bootstrap_password(home, log_path)
        if not password:
            fail("could not read Studio bootstrap password")
        await run_browser_probe(base_url, password, artifact_dir)
    finally:
        stop_process(proc)


if __name__ == "__main__":
    asyncio.run(main())
