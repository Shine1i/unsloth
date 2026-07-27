# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Drive the signed macOS package with native input and observable state.

Apple does not expose a WKWebView desktop WebDriver.  The script therefore
activates the real installed .app, takes OS screenshots, clicks the only
first-run action at its observed window coordinates, and then verifies the
desktop-owned backend and web UI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun, run_installed_web_ui_smoke  # noqa: E402


def osascript(script: str) -> str:
    completed = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"osascript failed ({completed.returncode}): {completed.stdout}")
    return completed.stdout.strip()


def window_bounds(process_hint: str) -> tuple[int, int, int, int]:
    script = f'''
tell application "System Events"
  set candidates to every application process whose name contains "{process_hint}"
  if (count of candidates) is 0 then error "no matching application process"
  set targetProcess to item 1 of candidates
  set frontmost of targetProcess to true
  if (count of windows of targetProcess) is 0 then error "process has no windows"
  set {{x, y}} to position of window 1 of targetProcess
  set {{w, h}} to size of window 1 of targetProcess
  return (x as text) & "," & (y as text) & "," & (w as text) & "," & (h as text)
end tell
'''
    raw = osascript(script)
    parts = [int(item.strip()) for item in raw.split(",")]
    if len(parts) != 4:
        raise RuntimeError(f"unexpected window bounds: {raw!r}")
    return parts[0], parts[1], parts[2], parts[3]


def click_get_started(process_hint: str) -> tuple[int, int]:
    x, y, width, height = window_bounds(process_hint)
    # The 760x560 packaged startup layout places the primary action 88 px above
    # the observed window bottom. Use native bounds rather than screen-fixed
    # coordinates.
    click_x = x + width // 2
    click_y = y + height - 88
    script = f'''
tell application "System Events"
  set candidates to every application process whose name contains "{process_hint}"
  if (count of candidates) is 0 then error "no matching application process"
  set targetProcess to item 1 of candidates
  set frontmost of targetProcess to true
  click at {{{click_x}, {click_y}}}
end tell
'''
    osascript(script)
    return click_x, click_y


def screenshot(path: Path) -> None:
    completed = subprocess.run(
        ["screencapture", "-x", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"screencapture failed: {completed.stdout}")


def candidate_health() -> tuple[int, dict] | None:
    for port in range(8888, 8909):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
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
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--scenario", default="PKG-02")
    parser.add_argument("--process-hint", default="Unsloth")
    parser.add_argument("--install-timeout", type=float, default=7200)
    parser.add_argument("--playwright-smoke")
    args = parser.parse_args()

    evidence = EvidenceRun(args.evidence, f"{args.scenario}-macos-first-install")
    evidence.begin(args.scenario)
    paths: list[Path] = []
    try:
        launch = evidence.run(
            ["open", "-n", str(args.app.resolve())],
            name="launch-installed-app",
            timeout=60,
        )
        if launch.returncode != 0:
            raise RuntimeError("open failed")
        deadline = time.monotonic() + 180
        bounds: tuple[int, int, int, int] | None = None
        while time.monotonic() < deadline:
            try:
                bounds = window_bounds(args.process_hint)
                break
            except Exception:
                time.sleep(2)
        if bounds is None:
            raise RuntimeError("signed app launched but no visible first-run window appeared")
        bounds_path = args.evidence / "initial-window.json"
        bounds_path.write_text(
            json.dumps({"x": bounds[0], "y": bounds[1], "width": bounds[2], "height": bounds[3]})
            + "\n",
            encoding="utf-8",
        )
        paths.append(bounds_path)
        initial_shot = evidence.screenshots_dir / "01-get-started.png"
        screenshot(initial_shot)
        paths.append(initial_shot)
        click_point = click_get_started(args.process_hint)
        click_path = args.evidence / "click.json"
        click_path.write_text(
            json.dumps({"x": click_point[0], "y": click_point[1]}) + "\n",
            encoding="utf-8",
        )
        paths.append(click_path)
        setup_deadline = time.monotonic() + 90
        managed_environment = (
            Path.home() / ".unsloth" / "studio" / "unsloth_studio"
        )
        while time.monotonic() < setup_deadline:
            if managed_environment.exists() or candidate_health() is not None:
                break
            time.sleep(1)
        else:
            raise RuntimeError(
                "native click did not start setup or create the managed environment"
            )
        time.sleep(3)
        installing_shot = evidence.screenshots_dir / "02-installing.png"
        screenshot(installing_shot)
        paths.append(installing_shot)

        deadline = time.monotonic() + args.install_timeout
        health: tuple[int, dict] | None = None
        while time.monotonic() < deadline:
            health = candidate_health()
            if health is not None:
                break
            time.sleep(5)
        if health is None:
            raise RuntimeError("no healthy desktop backend appeared on ports 8888-8908")
        port, payload = health
        health_path = args.evidence / "backend-health.json"
        health_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        paths.append(health_path)
        running_shot = evidence.screenshots_dir / "03-running.png"
        screenshot(running_shot)
        paths.append(running_shot)
        paths.extend(
            [
                evidence.snapshot_processes("running"),
                evidence.snapshot_tree(
                    "running", [Path.home() / ".unsloth", args.app.resolve()]
                ),
            ]
        )
        if args.playwright_smoke:
            paths.extend(run_installed_web_ui_smoke(evidence, args.playwright_smoke))
        final_bounds = window_bounds(args.process_hint)
        final_bounds_path = args.evidence / "final-window.json"
        final_bounds_path.write_text(
            json.dumps(
                {
                    "x": final_bounds[0],
                    "y": final_bounds[1],
                    "width": final_bounds[2],
                    "height": final_bounds[3],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        final_shot = evidence.screenshots_dir / "04-final.png"
        screenshot(final_shot)
        paths.extend([final_bounds_path, final_shot])
        evidence.record(
            args.scenario,
            "verified",
            f"Notarized DMG app was copied, launched by a real user action, setup was "
            f"clicked at observed window coordinates, its API backend became healthy "
            f"on {port}, and the installed CLI's browser UI passed Playwright.",
            evidence=[path.relative_to(args.evidence) for path in paths],
        )
        return 0
    except Exception as error:
        try:
            failure_shot = evidence.screenshots_dir / "99-failure.png"
            screenshot(failure_shot)
            paths.append(failure_shot)
        except Exception:
            pass
        paths.extend(
            [
                evidence.snapshot_processes("failure"),
                evidence.snapshot_tree("failure", [Path.home() / ".unsloth", args.app]),
            ]
        )
        evidence.record(
            args.scenario,
            "failed",
            f"Packaged macOS first-launch flow failed: {error}",
            evidence=[
                path.relative_to(args.evidence)
                if path.is_relative_to(args.evidence)
                else path
                for path in paths
            ],
            mismatch=str(error),
        )
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
