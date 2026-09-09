# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Drive an installed, unmodified official macOS 806 app through its own updater.

Requires a logged-in Aqua session, Accessibility permission for the runner/osascript,
Automation permission for System Events, and a writable .app installed from the DMG.
The parent must provision backend 2026.9.2 in the *actual* ~/.unsloth/studio: this
release's native shell does not honor UNSLOTH_STUDIO_HOME. --studio-home accepts
that directory or its .unsloth parent, not an alternate isolation mechanism.

No WebDriver, injected JavaScript, TCC edits, installer fallback, or forced relaunch.
Exit 0 proves observed staging, an AX update click, shell replacement, a new GUI
process and live backend metadata activation; it does not prove API health. Exit 2
is blocked/inconclusive. Run in a disposable macOS account with no other Studio.
Artifacts contain structured timings and a privacy-filtered AX tree, never control
values, arbitrary labels, screenshots, credentials, or wholesale application logs.

python mac_driver.py --app /Applications/Unsloth.app/Contents/MacOS/unsloth \
    --artifacts ./artifacts/mac --studio-home ~/.unsloth --timeout 1800
"""

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time


# Only exact, known English update controls are actionable. Unknown UI fails closed.
BUTTONS = ("Restart", "Finish update", "Update", "Update now", "Check for updates")
AX_SCRIPT = r"""
on run argv
    set bundleID to item 1 of argv
    set wanted to item 2 of argv
    tell application "System Events"
        if not UI elements enabled then error "Accessibility unavailable" number -1719
        set candidates to application processes whose bundle identifier is bundleID
        if (count of candidates) is 0 then return "NO_PROCESS"
        if (count of candidates) is not 1 then error "Ambiguous app processes" number -1728
        set p to item 1 of candidates
        set frontmost of p to true
        set report to "PID " & (unix id of p as text) & linefeed
        set elements to entire contents of p
        set seen to 0
        repeat with e in elements
            set seen to seen + 1
            if seen > 2500 then exit repeat
            set r to "unknown"
            set labelText to "[redacted]"
            set isEnabled to false
            try
                set r to role of e as text
                set isEnabled to enabled of e
                if r is "AXButton" or r is "AXMenuItem" then
                    set n to name of e as text
                    if n is in {"Restart", "Finish update", "Update", "Update now", "Check for updates"} then set labelText to n
                    if labelText is "[redacted]" then
                        set n to description of e as text
                        if n is in {"Restart", "Finish update", "Update", "Update now", "Check for updates"} then set labelText to n
                    end if
                end if
            end try
            set report to report & seen & " " & r & " enabled=" & isEnabled & " " & labelText & linefeed
            if wanted is not "" and labelText is wanted and isEnabled then
                perform action "AXPress" of e
                return report & "CLICKED " & wanted
            end if
        end repeat
        return report
    end tell
end run
"""


class Blocked(Exception):
    pass


def version(value):
    """Only publish version-shaped strings, never arbitrary marker content."""
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)+(?:-beta)?", value)
        else None
    )


def shell_version(bundle):
    try:
        with (bundle / "Contents/Info.plist").open("rb") as stream:
            return version(plistlib.load(stream).get("CFBundleShortVersionString"))
    except (OSError, plistlib.InvalidFileException):
        return None


def backend_version(home):
    versions = set()
    for metadata in home.glob(
        "unsloth_studio/lib/python*/site-packages/unsloth-*.dist-info/METADATA"
    ):
        try:
            for line in metadata.read_text().splitlines():
                if line.startswith("Version: "):
                    value = version(line.removeprefix("Version: "))
                    if value:
                        versions.add(value)
                    break
        except OSError:
            pass  # Atomic runtime swaps can invalidate a just-enumerated path.
    return next(iter(versions)) if len(versions) == 1 else None


def stage_versions(home):
    try:
        data = json.loads((home / ".update-stage/READY.json").read_text())
        return {key: version(data.get(key)) for key in ("shell_version", "backend_version")}
    except (OSError, ValueError, AttributeError):
        return {}


def ax(bundle_id, wanted=""):
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", AX_SCRIPT, bundle_id, wanted],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "AX_ERROR timeout"
    if result.returncode:
        # osascript errors may embed UI content: retain only the numeric error code.
        codes = re.findall(r"\(-?\d+\)", result.stderr)
        return "AX_ERROR " + (codes[-1] if codes else str(result.returncode))
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--studio-home", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    artifacts = args.artifacts.expanduser().resolve()
    home = args.studio_home.expanduser().resolve()
    if (home / "studio").is_dir():
        home = home / "studio"
    if artifacts == home or home in artifacts.parents:
        parser.error("artifacts must be outside the Studio runtime")
    artifacts.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    events = []
    bundle_id = None
    last_tree = "AX_NOT_ATTEMPTED"
    result = {"status": "blocked", "api_health_verified": False}

    def record(kind, **fields):
        events.append(
            {"elapsed_seconds": round(time.monotonic() - started, 3), "event": kind, **fields}
        )
        (artifacts / "mac-events.json").write_text(json.dumps(events, indent=2) + "\n")

    try:
        if sys.platform != "darwin":
            raise Blocked("Requires macOS Aqua and System Events; native validation not run")
        if home != (Path.home() / ".unsloth/studio").resolve():
            raise Blocked("Official shell uses ~/.unsloth/studio; provision that runtime first")
        app = args.app.expanduser().resolve()
        bundle = next((p for p in (app, *app.parents) if p.suffix == ".app"), None)
        if bundle is None:
            raise Blocked("--app must be an installed .app or its executable")
        with (bundle / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        bundle_id = info["CFBundleIdentifier"]
        executable = bundle / "Contents/MacOS" / info["CFBundleExecutable"]
        if app != bundle and app != executable:
            raise Blocked("--app is not the bundle's declared executable")
        before = {"shell": shell_version(bundle), "backend": backend_version(home)}
        record("baseline", **before)
        if before != {"shell": "0.1.806-beta", "backend": "2026.9.2"}:
            raise Blocked("Expected official shell 0.1.806-beta and backend 2026.9.2")
        if any((home / name).exists() for name in (".update-stage", ".update-prev")):
            raise Blocked("Pre-existing staging/activation state makes this run ambiguous")
        last_tree = ax(bundle_id)
        if last_tree != "NO_PROCESS":
            raise Blocked("App already running or Accessibility/Automation unavailable")
        env = os.environ.copy()
        # No output pipe/file: app logs can contain auth URLs and credentials.
        process = subprocess.Popen(
            [str(executable)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        record("launched", pid=process.pid)
        deadline = started + args.timeout
        clicked = set()
        staged = False
        stable_since = None
        previous = None
        while time.monotonic() < deadline:
            stage = stage_versions(home)
            if stage == {"shell_version": "0.1.807-beta", "backend_version": "2026.9.3"}:
                staged = True
            last_tree = ax(bundle_id)
            pid_match = re.match(r"PID (\d+)", last_tree)
            gui_pid = int(pid_match[1]) if pid_match else None
            state = {
                "shell": shell_version(bundle),
                "backend": backend_version(home),
                "stage": stage,
                "gui_pid": gui_pid,
                "pending": (home / ".update-prev/PENDING.json").exists(),
            }
            if state != previous:
                record("observed", **state)
                previous = state
            (artifacts / "mac-ax-tree.txt").write_text(last_tree + "\n")
            if last_tree.startswith("AX_ERROR"):
                raise Blocked(
                    "AX unavailable; grant runner Accessibility and System Events Automation"
                )
            if (home / ".update-failed.json").exists():
                raise Blocked("Native updater recorded .update-failed.json; update not verified")
            activated = (
                staged
                and bool(clicked & {"Restart", "Finish update", "Update", "Update now"})
                and state["shell"] == "0.1.807-beta"
                and state["backend"] == "2026.9.3"
                and gui_pid is not None
                and gui_pid != process.pid
                and not state["pending"]
            )
            if activated:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 20:
                    result.update(
                        status="passed", reason="Native update and runtime activation observed"
                    )
                    return 0
            else:
                stable_since = None
            if state["shell"] == "0.1.806-beta":
                for label in BUTTONS:
                    if label in clicked:
                        continue
                    if f"enabled=true {label}\n" not in last_tree + "\n":
                        continue
                    # Capture READY before a restart can consume it; never install manually.
                    if label in ("Restart", "Finish update") and not staged:
                        continue
                    response = ax(bundle_id, label)
                    if response.endswith("CLICKED " + label):
                        clicked.add(label)
                        record("ax_click", label=label)
                        break
            time.sleep(min(3, max(0, deadline - time.monotonic())))
        raise Blocked(
            "Deadline: no complete native update proof; inspect timings and redacted AX tree. "
            "Login/onboarding, inaccessible WKWebView controls or updater failure may block CI"
        )
    except Blocked as error:
        result["reason"] = str(error)
        return 2
    except (OSError, KeyError, ValueError) as error:
        result["reason"] = "Driver prerequisite/read failure: " + type(error).__name__
        return 2
    finally:
        # Do not kill/relaunch: that would substitute for the updater's own restart.
        (artifacts / "mac-ax-tree.txt").write_text(last_tree + "\n")
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        record("result", **result)
        (artifacts / "mac-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    sys.exit(main())
