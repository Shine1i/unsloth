# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Drive an installed official Windows 806 executable through real UIA controls.

Same --app, --studio-home, --artifacts, --timeout interface as native_driver.py.
Requires Windows PowerShell 5.1/.NET UIAutomation, WebView2 and an unlocked,
interactive desktop at the app's integrity level. Provision the actual native
~/.unsloth/studio with backend 2026.9.2 first; alternate homes are not supported.
The caller owns official binary provenance and disposable-account cleanup.

No CDP, injected JS, source build, installer fallback, forced restart, screenshots,
arbitrary UI text or application logs. Only exact English update controls are
invoked. Exit 2 is blocked/inconclusive, never a successful substitute for UI.
Success requires READY 807/.3, a UI install action, changed PE bytes and native
file version, a new GUI PID and new managed backend listener with healthy API.
The backend version comes from live runtime metadata, not the health endpoint.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request


BUTTONS = ("Restart", "Finish update", "Update", "Update now", "Check for updates")
TARGET = {"shell_version": "0.1.807-beta", "backend_version": "2026.9.3"}
# Paths and labels travel as environment values, never executable PowerShell text.
# Only allowlisted names leave this script. Never output exceptions (they can
# contain element names), command lines, process paths or window titles.
PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $app = $env:UNSLOTH_UIA_APP
    $runtime = $env:UNSLOTH_UIA_RUNTIME.TrimEnd('\') + '\'
    $wanted = $env:UNSLOTH_UIA_LABEL
    $allowed = @('Restart', 'Finish update', 'Update', 'Update now', 'Check for updates')
    if ($wanted -and $allowed -cnotcontains $wanted) { throw 'invalid label' }
    $v = [Diagnostics.FileVersionInfo]::GetVersionInfo($app)
    $fileVersion = '{0}.{1}.{2}.{3}' -f $v.FileMajorPart,$v.FileMinorPart,$v.FileBuildPart,$v.FilePrivatePart
    $productVersion = $null
    if ($v.ProductVersion -cmatch '^\d+(\.\d+)+(-beta)?$') { $productVersion = $v.ProductVersion }
    $processes = @(Get-Process | Where-Object {
        try { $_.Path -and [string]::Equals($_.Path, $app, [StringComparison]::OrdinalIgnoreCase) }
        catch { $false }
    })
    $gui = @($processes | Where-Object { $_.MainWindowHandle -ne 0 })
    if ($gui.Count -gt 1) { throw 'ambiguous GUI' }
    $backend = @()
    # Inspect listener ownership, not command lines. No output on unrelated ports.
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq 8888 })
    foreach ($connection in $listeners) {
        $owner = Get-Process -Id $connection.OwningProcess -ErrorAction Stop
        if ($owner.Path -and $owner.Path.StartsWith($runtime, [StringComparison]::OrdinalIgnoreCase)) {
            $backend += [int]$owner.Id
        } else { throw 'foreign backend listener' }
    }
    $labels = @()
    $matches = @()
    $guiPid = $null
    if ($gui.Count -eq 1) {
        $guiPid = [int]$gui[0].Id
        $root = [Windows.Automation.AutomationElement]::FromHandle($gui[0].MainWindowHandle)
        $condition = [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::ControlTypeProperty,
            [Windows.Automation.ControlType]::Button)
        $elements = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $condition)
        if ($elements.Count -gt 2500) { throw 'oversized tree' }
        foreach ($element in $elements) {
            $current = $element.Current
            if ($allowed -ccontains $current.Name -and $current.IsEnabled -and -not $current.IsOffscreen) {
                $pattern = $null
                if ($element.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                    $labels += $current.Name
                    if ($wanted -and $current.Name -ceq $wanted) { $matches += $pattern }
                }
            }
        }
    }
    $clicked = $null
    if ($wanted) {
        if ($matches.Count -ne 1) { throw 'missing or ambiguous control' }
        # Synchronous invocation can exit the app. If the provider does not return
        # successfully, proof is deliberately withheld rather than assuming click.
        $matches[0].Invoke()
        $clicked = $wanted
    }
    @{
        file_version=$fileVersion; product_version=$productVersion;
        app_pids=@($processes | ForEach-Object { [int]$_.Id }); gui_pid=$guiPid;
        backend_pids=@($backend | Sort-Object -Unique);
        labels=@($labels | Sort-Object -Unique); clicked=$clicked
    } | ConvertTo-Json -Compress -Depth 4
} catch { [Console]::Out.WriteLine('{"error":"UIA_UNAVAILABLE"}'); exit 2 }
"""


class Blocked(Exception):
    pass


def version(value):
    return value if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)+(?:-beta)?", value) else None


def backend_version(home):
    versions = set()
    for metadata in home.glob("unsloth_studio/Lib/site-packages/unsloth-*.dist-info/METADATA"):
        try:
            for line in metadata.read_text(encoding="utf-8").splitlines():
                if line.startswith("Version: "):
                    value = version(line[9:])
                    if value:
                        versions.add(value)
                    break
        except OSError:
            pass
    return next(iter(versions)) if len(versions) == 1 else None


def stage_versions(home):
    try:
        data = json.loads((home / ".update-stage/READY.json").read_text(encoding="utf-8"))
        return {key: version(data.get(key)) for key in TARGET}
    except (OSError, ValueError, AttributeError):
        return {}


def shell_matches(snapshot, build):
    # PE's numeric version cannot represent '-beta'. ProductVersion may retain
    # it, but must never contradict the numeric native file version.
    return snapshot.get("file_version") == f"0.1.{build}.0" and snapshot.get(
        "product_version"
    ) in (None, f"0.1.{build}", f"0.1.{build}.0", f"0.1.{build}-beta")


def sha256(app):
    with app.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def uia(app, home, label="", timeout=25):
    if label and label not in BUTTONS:
        raise Blocked("Non-allowlisted UI action refused")
    env = os.environ.copy()
    env.update(UNSLOTH_UIA_APP=str(app), UNSLOTH_UIA_RUNTIME=str(home / "unsloth_studio"),
               UNSLOTH_UIA_LABEL=label)
    encoded = base64.b64encode(PS_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        response = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
             "-EncodedCommand", encoded], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if response.returncode:
            raise Blocked("Windows UI Automation or native process inspection unavailable")
        value = json.loads(response.stdout)
        if not isinstance(value, dict) or value.get("error"):
            raise Blocked("Windows UI Automation unavailable")
        return value
    except (subprocess.TimeoutExpired, ValueError, OSError):
        raise Blocked("Windows UI Automation probe failed or timed out") from None


def healthy():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8888/api/health", timeout=3) as response:
            value = json.load(response)
        return value.get("status") == "healthy" and value.get("service") == "Unsloth UI Backend"
    except (OSError, ValueError, AttributeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--studio-home", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    app = args.app.expanduser().resolve()
    home = args.studio_home.expanduser().resolve()
    if (home / "studio").is_dir():
        home /= "studio"
    artifacts = args.artifacts.expanduser().resolve()
    if artifacts == home or home in artifacts.parents:
        parser.error("artifacts must be outside the Studio runtime")
    artifacts.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + args.timeout
    events = []
    result = {"status": "blocked", "api_health_verified": False}

    def record(event, **fields):
        events.append({"elapsed_seconds": round(time.monotonic() - started, 3),
                       "event": event, **fields})
        (artifacts / "windows-ui-events.json").write_text(json.dumps(events, indent=2) + "\n")

    def probe(label=""):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Blocked("Overall deadline reached")
        return uia(app, home, label, timeout=min(25, remaining))

    try:
        if sys.platform != "win32":
            raise Blocked("Requires an interactive Windows desktop; native validation not run")
        if home != (Path.home() / ".unsloth/studio").resolve():
            raise Blocked("Official shell requires the actual ~/.unsloth/studio runtime")
        if not app.is_file() or app.suffix.lower() != ".exe":
            raise Blocked("Expected installed official Windows executable")
        if any((home / name).exists() for name in
               (".update-stage", ".update-prev", ".update-failed.json")):
            raise Blocked("Pre-existing update state makes activation ambiguous")
        baseline = probe()
        if baseline["app_pids"] or baseline["backend_pids"]:
            raise Blocked("App or managed backend already running")
        if not shell_matches(baseline, 806) or backend_version(home) != "2026.9.2":
            raise Blocked("Expected native shell 806 and backend 2026.9.2")
        before_hash = sha256(app)
        record("baseline", file_version=baseline["file_version"], backend="2026.9.2",
               sha256=before_hash)
        env = os.environ.copy()
        env.pop("UNSLOTH_STUDIO_HOME", None)
        env.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
        process = subprocess.Popen([str(app)], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        record("launched", pid=process.pid)
        old_backend_pids = set()
        staged = False
        clicked = set()
        stable_since = None
        previous = None
        while time.monotonic() < deadline:
            stage = stage_versions(home)
            if stage == TARGET:
                staged = True
            elif stage and any(stage.values()):
                raise Blocked("Staged update is not the exact 807/backend .3 target")
            snapshot = probe()
            backend = backend_version(home)
            pending = (home / ".update-prev/PENDING.json").exists()
            state = {**snapshot, "backend": backend, "stage": stage, "pending": pending}
            if state != previous:
                record("observed", **state)
                previous = state
            if (home / ".update-failed.json").exists():
                raise Blocked("Native updater recorded activation failure")
            if not clicked and shell_matches(snapshot, 806) and backend == "2026.9.2":
                old_backend_pids.update(snapshot["backend_pids"])
            activated = (
                staged and bool(clicked - {"Check for updates"})
                and shell_matches(snapshot, 807) and backend == "2026.9.3"
                and snapshot["gui_pid"] is not None and snapshot["gui_pid"] != process.pid
                and process.poll() is not None and bool(old_backend_pids)
                and bool(snapshot["backend_pids"])
                and not old_backend_pids.intersection(snapshot["backend_pids"])
                and not pending and sha256(app) != before_hash and healthy()
            )
            if activated:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 20:
                    result.update(status="passed", api_health_verified=True,
                                  reason="Native UI update and runtime activation observed",
                                  before_sha256=before_hash, after_sha256=sha256(app))
                    return 0
            else:
                stable_since = None
            if shell_matches(snapshot, 806) and old_backend_pids:
                for label in BUTTONS:
                    if label in clicked or label not in snapshot["labels"]:
                        continue
                    # Every mutating action waits for the exact READY target.
                    if label != "Check for updates" and not staged:
                        continue
                    response = probe(label)
                    if response.get("clicked") != label:
                        raise Blocked("UI action was not acknowledged")
                    clicked.add(label)
                    record("uia_click", label=label, pid=snapshot["gui_pid"])
                    break
            time.sleep(min(3, max(0, deadline - time.monotonic())))
        raise Blocked("Deadline: update controls or complete native activation proof unavailable")
    except Blocked as error:
        result["reason"] = str(error)
        return 2
    except Exception as error:
        # Exception text may include private paths or UI data. Publish type only.
        result["reason"] = "Driver prerequisite/read failure: " + type(error).__name__
        return 2
    finally:
        # Leave process cleanup to the caller; never substitute our own relaunch.
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        record("result", **result)
        (artifacts / "windows-ui-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    sys.exit(main())
