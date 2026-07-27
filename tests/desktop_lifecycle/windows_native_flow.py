# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Drive the installed Windows package with native window input.

WebView2/EdgeDriver combinations on hosted Windows can fail before creating a
DevTools session even while the real Tauri window is healthy. This fallback
uses the same bounded coordinate technique as the macOS flow: discover the
actual HWND, capture the desktop, click the setup screen's sole primary action,
then verify the backend and installed CLI browser UI independently.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun, quote_command, run_installed_web_ui_smoke  # noqa: E402
from tauri_driver_flow import collect_tauri_runtime_logs  # noqa: E402


user32 = ctypes.windll.user32


def find_window(pid: int) -> int | None:
    windows: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return windows[0] if windows else None


def window_bounds(hwnd: int) -> tuple[int, int, int, int]:
    rectangle = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rectangle)):
        raise RuntimeError(f"GetWindowRect failed for HWND {hwnd}")
    return (
        rectangle.left,
        rectangle.top,
        rectangle.right - rectangle.left,
        rectangle.bottom - rectangle.top,
    )


def screenshot(path: Path) -> None:
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=True)
    image.save(path)


def click_get_started(hwnd: int) -> tuple[int, int]:
    x, y, width, height = window_bounds(hwnd)
    click_x = x + width // 2
    click_y = y + height - 88
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    if not user32.SetCursorPos(click_x, click_y):
        raise RuntimeError(f"SetCursorPos failed for {click_x},{click_y}")
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    return click_x, click_y


def candidate_health() -> tuple[int, dict] | None:
    for port in range(8888, 8909):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1
            ) as response:
                payload = json.loads(
                    response.read().decode("utf-8", errors="replace")
                )
            if (
                payload.get("status") == "healthy"
                and payload.get("service") == "Unsloth UI Backend"
            ):
                return port, payload
        except Exception:
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--scenario", default="PKG-01")
    parser.add_argument("--install-timeout", type=float, default=7200)
    parser.add_argument("--playwright-smoke")
    args = parser.parse_args()
    args.application = args.application.resolve()
    output = args.evidence.resolve()
    evidence = EvidenceRun(output, f"{args.scenario}-windows-native-first-install")
    evidence.begin(args.scenario)
    paths: list[Path] = []
    process: subprocess.Popen[str] | None = None
    log_handle = None

    try:
        user32.SetProcessDPIAware()
        application_log = evidence.logs_dir / "windows-application.log"
        command_path = output / "application-command.txt"
        command_path.write_text(quote_command([str(args.application)]) + "\n", encoding="utf-8")
        paths.extend([command_path, application_log])
        log_handle = application_log.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [str(args.application)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        deadline = time.monotonic() + 180
        hwnd: int | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Installed Windows app exited {process.returncode} before showing a window"
                )
            hwnd = find_window(process.pid)
            if hwnd is not None:
                break
            time.sleep(1)
        if hwnd is None:
            raise RuntimeError("Installed Windows app launched but no visible HWND appeared")

        bounds = window_bounds(hwnd)
        bounds_path = output / "initial-window.json"
        bounds_path.write_text(
            json.dumps(
                {
                    "hwnd": hwnd,
                    "pid": process.pid,
                    "x": bounds[0],
                    "y": bounds[1],
                    "width": bounds[2],
                    "height": bounds[3],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        paths.append(bounds_path)
        time.sleep(3)
        initial_shot = evidence.screenshots_dir / "01-get-started.png"
        screenshot(initial_shot)
        paths.append(initial_shot)

        click_point = click_get_started(hwnd)
        click_path = output / "click.json"
        click_path.write_text(
            json.dumps({"x": click_point[0], "y": click_point[1]}) + "\n",
            encoding="utf-8",
        )
        paths.append(click_path)
        setup_deadline = time.monotonic() + 90
        managed_root = Path.home() / ".unsloth" / "studio"
        while time.monotonic() < setup_deadline:
            if managed_root.exists() or candidate_health() is not None:
                break
            if process.poll() is not None:
                raise RuntimeError(
                    f"Installed Windows app exited {process.returncode} after setup click"
                )
            time.sleep(1)
        else:
            raise RuntimeError(
                "native click did not start setup or create the managed root"
            )
        time.sleep(3)
        if process.poll() is not None:
            raise RuntimeError(
                f"Installed Windows app exited {process.returncode} after setup click"
            )
        installing_shot = evidence.screenshots_dir / "02-installing.png"
        screenshot(installing_shot)
        paths.append(installing_shot)

        deadline = time.monotonic() + args.install_timeout
        health: tuple[int, dict] | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Installed Windows app exited {process.returncode} during setup"
                )
            health = candidate_health()
            if health is not None:
                break
            time.sleep(5)
        if health is None:
            raise RuntimeError("No healthy desktop backend appeared on ports 8888-8908")

        port, payload = health
        health_path = output / "backend-health.json"
        health_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        paths.append(health_path)
        time.sleep(3)
        hwnd = find_window(process.pid)
        if process.poll() is not None or hwnd is None:
            raise RuntimeError("Installed Windows app did not remain alive after backend startup")
        running_shot = evidence.screenshots_dir / "03-running.png"
        screenshot(running_shot)
        paths.append(running_shot)
        paths.extend(
            [
                evidence.snapshot_processes("running"),
                evidence.snapshot_tree(
                    "running",
                    [
                        Path.home() / ".unsloth" / "studio" / "share",
                        Path.home() / ".unsloth" / "studio" / "auth",
                        Path.home() / ".unsloth" / "studio" / "outputs",
                        args.application,
                    ],
                ),
            ]
        )
        if args.playwright_smoke:
            paths.extend(run_installed_web_ui_smoke(evidence, args.playwright_smoke))
        if process.poll() is not None or find_window(process.pid) is None:
            raise RuntimeError(
                "Installed Windows app exited during installed-CLI Playwright verification"
            )
        paths.extend(collect_tauri_runtime_logs(evidence))
        evidence.record(
            args.scenario,
            "verified",
            (
                "Signed NSIS app launched in the interactive desktop, Get Started "
                f"was clicked at observed HWND coordinates, backend {port} became "
                "healthy, the app stayed visible, and the installed CLI browser UI "
                "passed Playwright."
            ),
            evidence=[path.relative_to(output) for path in paths],
        )
    except Exception as error:
        paths.extend(collect_tauri_runtime_logs(evidence))
        try:
            failure_shot = evidence.screenshots_dir / "99-failure.png"
            screenshot(failure_shot)
            paths.append(failure_shot)
        except Exception:
            pass
        paths.extend(
            [
                evidence.snapshot_processes("failure"),
                evidence.snapshot_tree(
                    "failure",
                    [
                        Path.home() / ".unsloth" / "studio" / "share",
                        Path.home() / ".unsloth" / "studio" / "auth",
                        Path.home() / ".unsloth" / "studio" / "outputs",
                    ],
                ),
            ]
        )
        evidence.record(
            args.scenario,
            "failed",
            f"Packaged Windows first-launch flow failed: {error}",
            evidence=[
                path.relative_to(output) if path.is_relative_to(output) else path
                for path in paths
            ],
            mismatch=str(error),
        )
        print(error, file=sys.stderr)
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
        if log_handle is not None:
            log_handle.close()

    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
