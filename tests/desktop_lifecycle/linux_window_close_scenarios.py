# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Exercise closing and reopening the real Linux setup window.

The probe temporarily moves the existing managed root, starts a fresh packaged
setup, sends the X11 WM_DELETE_WINDOW event, and restores the original root.
It refuses to run outside a marked disposable home.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun  # noqa: E402
from linux_runtime_scenarios import (  # noqa: E402
    assert_disposable_home,
    pid_command,
    process_rows,
    record,
    wait_until,
)


def installer_pids() -> set[int]:
    return {
        pid
        for pid, command in process_rows()
        if "install.sh" in command and " --tauri" in f" {command} "
    }


def visible_windows(pid: int) -> list[str]:
    completed = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--pid", str(pid)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def window_geometry(window_id: str) -> dict[str, int]:
    completed = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", window_id],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    geometry: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"X", "Y", "WIDTH", "HEIGHT"}:
            geometry[key.lower()] = int(value)
    if set(geometry) != {"x", "y", "width", "height"}:
        raise RuntimeError(
            f"Could not parse X11 window geometry: {completed.stdout!r}"
        )
    return geometry


def screenshot(path: Path) -> None:
    completed = subprocess.run(
        ["scrot", "--silent", "--overwrite", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"scrot exited {completed.returncode}: {completed.stdout.strip()}"
        )


def stop_new_installers(pids: set[int]) -> None:
    for pid in pids:
        if pid_command(pid) is None:
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not any(pid_command(pid) is not None for pid in pids):
            return
        time.sleep(0.5)
    for pid in pids:
        if pid_command(pid) is None:
            continue
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tauri-driver", default="tauri-driver")
    args = parser.parse_args()
    args.application = args.application.resolve()

    home = assert_disposable_home()
    if args.application != Path("/usr/bin/unsloth-studio"):
        raise RuntimeError(f"Refusing unexpected application {args.application}")
    if shutil.which("xdotool") is None:
        raise RuntimeError("RUN-06 requires xdotool in the disposable environment")

    default_root = home / ".unsloth" / "studio"
    backup_root = home / ".unsloth" / "desktop-lifecycle-window-close-backup"
    if not default_root.is_dir() or backup_root.exists():
        raise RuntimeError("RUN-06 managed-root preconditions are not clean")

    evidence = EvidenceRun(args.evidence, "linux-window-close")
    evidence.begin("RUN-06")
    manifest = evidence.output_dir / "window-close-inputs.json"
    manifest.write_text(
        json.dumps(
            {
                "application": str(args.application),
                "default_root": str(default_root),
                "backup_root": str(backup_root),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    common_paths = [manifest]
    baseline_installers = installer_pids()
    new_installers: set[int] = set()
    application: subprocess.Popen[str] | None = None
    application_log_handle = None
    secondary: subprocess.Popen[str] | None = None
    secondary_log_handle = None
    root_moved = False

    try:
        default_root.rename(backup_root)
        root_moved = True
        environment = os.environ.copy()
        environment.setdefault("GDK_BACKEND", "x11")
        environment.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
        environment.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        application_log = evidence.logs_dir / "run06-application.log"
        application_log_handle = application_log.open("w", encoding="utf-8")
        application = subprocess.Popen(
            [str(args.application)],
            env=environment,
            stdout=application_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        app_pid = application.pid
        initial_windows = wait_until(
            lambda: visible_windows(app_pid),
            120,
            f"fresh setup window for PID {app_pid}",
        )
        geometry = window_geometry(initial_windows[0])
        geometry_path = evidence.output_dir / "run06-window-geometry.json"
        geometry_path.write_text(
            json.dumps(geometry, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        time.sleep(3)
        initial_shot = evidence.screenshots_dir / "run06-get-started.png"
        screenshot(initial_shot)
        click_x = geometry["x"] + geometry["width"] // 2
        click_y = geometry["y"] + geometry["height"] - 88
        click = evidence.run(
            [
                "xdotool",
                "mousemove",
                "--sync",
                str(click_x),
                str(click_y),
                "click",
                "1",
            ],
            name="run06-click-get-started",
            timeout=30,
            check=False,
        )
        if click.returncode != 0:
            raise RuntimeError("Native Get Started coordinate click failed")
        wait_until(
            lambda: installer_pids() - baseline_installers,
            120,
            "bundled --tauri installer process",
        )
        new_installers = installer_pids() - baseline_installers
        installing_shot = evidence.screenshots_dir / "run06-installing.png"
        screenshot(installing_shot)
        before_processes = evidence.snapshot_processes("run06-before-close")

        window_ids = wait_until(
            lambda: visible_windows(app_pid),
            30,
            f"visible X11 window for PID {app_pid}",
        )
        close_x = geometry["x"] + geometry["width"] - 24
        close_y = geometry["y"] + 17
        close = evidence.run(
            [
                "xdotool",
                "mousemove",
                "--sync",
                str(close_x),
                str(close_y),
                "click",
                "1",
            ],
            name="run06-click-window-close",
            timeout=30,
            check=False,
        )
        hidden = wait_until(
            lambda: visible_windows(app_pid) == [],
            30,
            "setup window to hide after WM_DELETE_WINDOW",
        )
        time.sleep(5)
        installers_after_close = {
            pid for pid in new_installers if pid_command(pid) is not None
        }
        application_survived_close = application.poll() is None
        hidden_shot = evidence.screenshots_dir / "run06-hidden-desktop.png"
        screenshot(hidden_shot)
        hidden_processes = evidence.snapshot_processes("run06-hidden")

        reopened_windows: list[str] = []
        reopened_paths: list[Path] = []
        if application_survived_close:
            secondary_log = evidence.logs_dir / "run06-secondary-launch.log"
            secondary_log_handle = secondary_log.open("w", encoding="utf-8")
            secondary = subprocess.Popen(
                [str(args.application)],
                stdout=secondary_log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            reopened_windows = wait_until(
                lambda: visible_windows(app_pid),
                45,
                "single-instance relaunch to reopen setup window",
            )
            reopened_shot = evidence.screenshots_dir / "run06-reopened.png"
            screenshot(reopened_shot)
            reopened_processes = evidence.snapshot_processes("run06-reopened")
            reopened_paths.extend(
                [secondary_log, reopened_shot, reopened_processes]
            )
        # The window disappeared immediately while the installer stayed alive;
        # therefore no close-confirmation choice remained visible to the user.
        disclosure = False
        continued = bool(installers_after_close)
        reopened = bool(reopened_windows)
        passed = (
            close.returncode == 0
            and hidden
            and continued
            and reopened
            and disclosure
        )
        observation = evidence.output_dir / "run06-observations.json"
        observation.write_text(
            json.dumps(
                {
                    "app_pid": app_pid,
                    "installer_pids_before_close": sorted(new_installers),
                    "installer_pids_after_close": sorted(installers_after_close),
                    "application_survived_close": application_survived_close,
                    "window_hidden": hidden,
                    "single_instance_reopened_window": reopened,
                    "background_or_cancel_disclosure": disclosure,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        record(
            evidence,
            "RUN-06",
            passed,
            (
                "The real setup window hid on WM_DELETE_WINDOW while its "
                f"installer continued={continued}; relaunch reopened={reopened}, "
                f"close/background disclosure={disclosure}."
            ),
            [
                *common_paths,
                application_log,
                geometry_path,
                initial_shot,
                installing_shot,
                before_processes,
                hidden_shot,
                hidden_processes,
                *reopened_paths,
                observation,
                evidence.logs_dir / "run06-click-get-started.log",
                evidence.logs_dir / "run06-click-window-close.log",
            ],
            mismatch=(
                "Closing the active setup window silently hid it and left setup "
                "running; there was no continue-in-background/cancel confirmation "
                "or visible background-progress disclosure."
            ),
        )
    except Exception as error:
        evidence.record(
            "RUN-06",
            "failed",
            f"Linux setup-window close sequence failed: {error}",
            evidence=[
                path.relative_to(evidence.output_dir) for path in common_paths
            ],
            mismatch=str(error),
        )
        print(f"window-close failure: {error}", file=sys.stderr)
    finally:
        if secondary is not None and secondary.poll() is None:
            try:
                os.killpg(secondary.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if secondary_log_handle is not None:
            secondary_log_handle.close()
        if application is not None and application.poll() is None:
            try:
                os.killpg(application.pid, signal.SIGTERM)
                application.wait(timeout=20)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(application.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if application_log_handle is not None:
            application_log_handle.close()
        new_installers.update(installer_pids() - baseline_installers)
        stop_new_installers(new_installers)
        if root_moved:
            if default_root.exists():
                shutil.rmtree(default_root)
            backup_root.rename(default_root)

    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
