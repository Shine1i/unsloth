# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Small Playwright smoke for the installed CLI's HTTP-served browser UI."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ["BASE_URL"].rstrip("/")
ARTIFACTS = Path(os.environ.get("PW_ART_DIR", "playwright-artifacts"))
ARTIFACTS.mkdir(parents=True, exist_ok=True)


def wait_health(timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if payload.get("status") == "healthy":
                return payload
        except Exception as error:
            last_error = error
        time.sleep(1)
    raise AssertionError(f"backend health timeout: {last_error}")


def main() -> None:
    health = wait_health()
    (ARTIFACTS / "health.json").write_text(
        json.dumps(health, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.goto(BASE, wait_until="domcontentloaded", timeout=120_000)
        if response is None or response.status >= 400:
            raise AssertionError(
                f"web UI navigation failed: {None if response is None else response.status}"
            )
        page.locator("body").wait_for(state="visible", timeout=60_000)
        actions: list[str] = []
        ephemeral_password = "DesktopAudit-2026!"
        new_password = page.locator("#new-password")
        try:
            new_password.wait_for(state="visible", timeout=15_000)
        except Exception:
            pass
        if new_password.is_visible():
            new_password.fill(ephemeral_password)
            page.locator("#confirm-password").fill(ephemeral_password)
            page.get_by_role("button", name="Change password").click()
            page.wait_for_function(
                "() => window.location.pathname !== '/change-password'",
                timeout=60_000,
            )
            actions.append("completed disposable first-run password setup")

        login_password = page.locator("#password")
        if login_password.is_visible():
            login_password.fill(ephemeral_password)
            page.get_by_role("button", name="Login").click()
            page.wait_for_function(
                "() => window.location.pathname !== '/login'",
                timeout=60_000,
            )
            actions.append("logged into disposable browser UI")

        body_text = page.locator("body").inner_text(timeout=60_000)
        if not body_text.strip():
            raise AssertionError("web UI body is blank")
        if "/chat" not in page.url or "New chat" not in body_text:
            raise AssertionError(
                f"web UI did not reach the chat shell: url={page.url!r}, "
                f"body={body_text[:500]!r}"
            )
        page.screenshot(
            path=str(ARTIFACTS / "web-ui.png"),
            full_page=True,
            animations="disabled",
            timeout=90_000,
        )
        (ARTIFACTS / "page.json").write_text(
            json.dumps(
                {
                    "url": page.url,
                    "title": page.title(),
                    "body_prefix": body_text[:4000],
                    "page_errors": errors,
                    "actions": actions,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if errors:
            raise AssertionError(f"web UI raised page errors: {errors[:3]}")
        browser.close()


if __name__ == "__main__":
    main()
