# SPDX-License-Identifier: AGPL-3.0-only
# Disposable PR #6520 repro: drive Settings -> General -> Change password.

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _playwright_robust import (  # noqa: E402
    chromium_launch_args,
    install_view_transition_killer,
    is_benign_console_error,
    is_benign_page_error,
    wait_for_health,
)

BASE = os.environ["BASE_URL"].rstrip("/")
BOOTSTRAP = os.environ["STUDIO_BOOTSTRAP_PW"]
BROWSERS = [b.strip() for b in os.environ.get("PW_BROWSERS", "chromium").split(",") if b.strip()]
ART = Path(os.environ.get("PW_ART_DIR", "logs/settings-password"))
ART.mkdir(parents=True, exist_ok=True)

AUTH_TOKEN_KEY = "unsloth_auth_token"
AUTH_REFRESH_TOKEN_KEY = "unsloth_auth_refresh_token"
AUTH_MUST_CHANGE_PASSWORD_KEY = "unsloth_auth_must_change_password"


def info(message: str) -> None:
    print(f"[settings-password] {message}", flush=True)


def fail(message: str) -> None:
    raise AssertionError(f"[settings-password] FAIL: {message}")


def post_json(path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"raw": raw}
        return exc.code, parsed


def api_login(password: str) -> tuple[int, dict]:
    return post_json("/api/auth/login", {"username": "unsloth", "password": password})


def api_change_password(token: str, current: str, new: str) -> tuple[int, dict]:
    return post_json(
        "/api/auth/change-password",
        {"current_password": current, "new_password": new},
        token=token,
    )


def assert_login_status(password: str, expected: int, label: str) -> None:
    status, body = api_login(password)
    if status != expected:
        fail(f"{label}: expected login status {expected}, got {status}, body={body!r}")


def bootstrap_to_base_password() -> str:
    base_password = f"PR6520-Base-{secrets.token_urlsafe(12)}"
    status, token = api_login(BOOTSTRAP)
    if status != 200:
        fail(f"bootstrap login failed: status={status}, body={token!r}")
    access = token.get("access_token")
    if not access:
        fail(f"bootstrap login returned no access_token: {token!r}")
    status, body = api_change_password(access, BOOTSTRAP, base_password)
    if status != 200:
        fail(f"bootstrap change-password failed: status={status}, body={body!r}")
    assert_login_status(BOOTSTRAP, 401, "bootstrap password after initial rotation")
    assert_login_status(base_password, 200, "base password after initial rotation")
    info("PASS bootstrap password rotated to a base password via API")
    return base_password


def browser_launcher(playwright, name: str):
    if name == "chromium":
        return playwright.chromium.launch(headless=True, args=chromium_launch_args())
    if name == "firefox":
        return playwright.firefox.launch(headless=True)
    if name == "webkit":
        return playwright.webkit.launch(headless=True)
    if name == "edge":
        return playwright.chromium.launch(headless=True, channel="msedge")
    fail(f"unknown browser {name!r}")


def inject_tokens(context, token: dict) -> None:
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    if not access or not refresh:
        fail(f"login token missing access/refresh token: {token!r}")
    payload = json.dumps(
        [access, refresh, AUTH_TOKEN_KEY, AUTH_REFRESH_TOKEN_KEY, AUTH_MUST_CHANGE_PASSWORD_KEY],
    )
    context.add_init_script(
        f"""
        (() => {{
          const [access, refresh, tokenKey, refreshKey, mustKey] = {payload};
          localStorage.setItem(tokenKey, access);
          localStorage.setItem(refreshKey, refresh);
          localStorage.removeItem(mustKey);
        }})();
        """,
    )


def open_settings_dialog(page) -> None:
    page.goto(f"{BASE}/settings", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(800)
    if page.get_by_role("dialog").first.count() == 0:
        page.keyboard.press("Control+,")
        page.wait_for_timeout(800)
    if page.get_by_role("dialog").first.count() == 0:
        page.keyboard.press("Meta+,")
        page.wait_for_timeout(800)
    dialog = page.get_by_role("dialog").first
    dialog.wait_for(state="visible", timeout=60_000)
    general = page.get_by_role("button", name="General").first
    if general.count() > 0:
        general.click()
        page.wait_for_timeout(300)
    page.get_by_role("button", name="Change password").first.click()
    page.locator("#cp-current").wait_for(state="visible", timeout=60_000)


def fill_settings_password_form(page, current: str, new: str) -> None:
    page.locator("#cp-current").fill(current)
    page.locator("#cp-new").fill(new)
    page.locator("#cp-confirm").fill(new)


def assert_page_still_authenticated(page, browser_name: str) -> None:
    status = page.evaluate(
        """
        async ([base, tokenKey]) => {
          const token = localStorage.getItem(tokenKey);
          if (!token) return -1;
          const response = await fetch(`${base}/api/chat/projects`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          return response.status;
        }
        """,
        [BASE, AUTH_TOKEN_KEY],
    )
    if status != 200:
        fail(f"{browser_name}: page token did not authenticate /api/chat/projects, status={status}")


def expected_console_error(message: str) -> bool:
    if is_benign_console_error(message):
        return True
    # This repro intentionally creates 401s for wrong-current-password and
    # old-password checks. Chromium reports those failed fetches as console
    # errors even though the HTTP status is the expected behavior.
    return "Failed to load resource" in message and (
        "401" in message or "Unauthorized" in message
    )


def run_browser(playwright, browser_name: str, current_password: str, next_password: str, simulate_empty_401: bool) -> None:
    status, token = api_login(current_password)
    if status != 200:
        fail(f"{browser_name}: login with current password failed before UI flow: {status}, {token!r}")

    browser = browser_launcher(playwright, browser_name)
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        reduced_motion="reduce",
    )
    install_view_transition_killer(context)
    inject_tokens(context, token)
    page = context.new_page()
    page.set_default_timeout(60_000)

    page_errors: list[str] = []
    console_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    refresh_hits = {"count": 0}

    def refresh_route(route):
        refresh_hits["count"] += 1
        route.continue_()

    page.route("**/api/auth/refresh", refresh_route)

    try:
        info(f"{browser_name}: open Settings change-password dialog")
        open_settings_dialog(page)
        page.screenshot(path=str(ART / f"{browser_name}-settings-dialog.png"), full_page=True)

        wrong_password = f"PR6520-Wrong-{secrets.token_urlsafe(8)}"
        fill_settings_password_form(page, wrong_password, next_password)
        before_wrong_refreshes = refresh_hits["count"]
        with page.expect_response(
            lambda response: "/api/auth/change-password" in response.url
            and response.request.method == "POST",
            timeout=30_000,
        ) as wrong_response:
            page.get_by_role("button", name="Update password").first.click()
        wrong_status = wrong_response.value.status
        if wrong_status != 401:
            fail(f"{browser_name}: wrong-current password should return 401, got {wrong_status}")
        page.wait_for_timeout(1000)
        if refresh_hits["count"] != before_wrong_refreshes:
            fail(f"{browser_name}: wrong-current password triggered refreshSession()")
        page.locator("#cp-current").wait_for(state="visible", timeout=30_000)
        assert_page_still_authenticated(page, browser_name)
        info(f"{browser_name}: PASS wrong current password did not refresh or log out")

        fake_empty_401 = {"used": False}
        if simulate_empty_401:

            def change_password_route(route):
                if (
                    not fake_empty_401["used"]
                    and route.request.method == "POST"
                    and "/api/auth/change-password" in route.request.url
                ):
                    fake_empty_401["used"] = True
                    route.fulfill(status=401, body="", headers={"Content-Type": "text/plain"})
                    return
                route.continue_()

            page.route("**/api/auth/change-password", change_password_route)

        refreshes_before_submit = refresh_hits["count"]
        fill_settings_password_form(page, current_password, next_password)
        page.get_by_role("button", name="Update password").first.click()
        page.locator("#cp-current").wait_for(state="detached", timeout=60_000)
        page.wait_for_timeout(1000)

        if simulate_empty_401:
            if not fake_empty_401["used"]:
                fail(f"{browser_name}: empty 401 simulation was not used")
            if refresh_hits["count"] <= refreshes_before_submit:
                fail(f"{browser_name}: empty 401 did not trigger refreshSession()")
            info(f"{browser_name}: PASS empty/non-JSON 401 refreshed and retried")
        else:
            if refresh_hits["count"] != refreshes_before_submit:
                fail(f"{browser_name}: successful change-password unexpectedly refreshed")

        assert_login_status(current_password, 401, f"{browser_name}: old password after Settings change")
        assert_login_status(next_password, 200, f"{browser_name}: new password after Settings change")
        assert_page_still_authenticated(page, browser_name)
        info(f"{browser_name}: PASS Settings password change old=401 new=200")

        real_page_errors = [e for e in page_errors if not is_benign_page_error(e)]
        real_console_errors = [e for e in console_errors if not expected_console_error(e)]
        if real_page_errors:
            fail(f"{browser_name}: non-benign page errors: {real_page_errors[:3]!r}")
        if real_console_errors:
            fail(f"{browser_name}: non-benign console errors: {real_console_errors[:3]!r}")
    finally:
        context.close()
        browser.close()


def main() -> None:
    wait_for_health(BASE, timeout=60, info=info)
    current_password = bootstrap_to_base_password()
    with sync_playwright() as playwright:
        for index, browser_name in enumerate(BROWSERS):
            next_password = f"PR6520-{browser_name}-{secrets.token_urlsafe(12)}"
            run_browser(
                playwright,
                browser_name,
                current_password,
                next_password,
                simulate_empty_401=(index == 0),
            )
            current_password = next_password
    info("PASS all requested browser Settings password repros")


if __name__ == "__main__":
    main()
