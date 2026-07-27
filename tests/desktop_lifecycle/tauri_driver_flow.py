# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Drive an installed Tauri binary through tauri-driver on Linux or Windows.

The production binary is used as-is.  No webdriver plugin, mock command, test
route, or alternate frontend is compiled into the application.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun, run_installed_web_ui_smoke  # noqa: E402


class WebDriverError(RuntimeError):
    pass


class WebDriver:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id: str | None = None

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise WebDriverError(f"{method} {endpoint}: HTTP {error.code}: {detail}") from error
        except Exception as error:
            raise WebDriverError(f"{method} {endpoint}: {error}") from error
        value = body.get("value", body)
        if isinstance(value, dict) and value.get("error"):
            raise WebDriverError(
                f"{method} {endpoint}: {value.get('error')}: {value.get('message')}"
            )
        return value

    def wait_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.request("GET", "/status", timeout=2)
                return
            except Exception as error:
                last_error = error
                time.sleep(0.5)
        raise WebDriverError(f"tauri-driver did not become ready: {last_error}")

    def start_session(self, application: Path) -> None:
        capabilities = {
            "capabilities": {
                "alwaysMatch": {
                    "tauri:options": {"application": str(application.resolve())}
                }
            }
        }
        value = self.request("POST", "/session", capabilities, timeout=120)
        if isinstance(value, dict):
            self.session_id = value.get("sessionId")
        if not self.session_id:
            raise WebDriverError(f"New session response had no sessionId: {value!r}")

    def endpoint(self, suffix: str) -> str:
        if not self.session_id:
            raise WebDriverError("No WebDriver session")
        return f"/session/{self.session_id}{suffix}"

    def source(self) -> str:
        return str(self.request("GET", self.endpoint("/source"), timeout=30))

    def screenshot(self, path: Path) -> None:
        encoded = self.request("GET", self.endpoint("/screenshot"), timeout=20)
        path.write_bytes(base64.b64decode(encoded))

    def find_xpath(self, xpath: str) -> str:
        value = self.request(
            "POST",
            self.endpoint("/element"),
            {"using": "xpath", "value": xpath},
            timeout=30,
        )
        if not isinstance(value, dict):
            raise WebDriverError(f"Unexpected element response: {value!r}")
        element = value.get("element-6066-11e4-a52e-4f735466cecf") or value.get("ELEMENT")
        if not element:
            raise WebDriverError(f"Element response had no id: {value!r}")
        return str(element)

    def click(self, element: str) -> None:
        self.request("POST", self.endpoint(f"/element/{element}/click"), {}, timeout=30)

    def close(self) -> None:
        if not self.session_id:
            return
        try:
            self.request("DELETE", self.endpoint(""), timeout=30)
        except Exception:
            pass
        self.session_id = None


def capture_desktop_screenshot(driver: WebDriver, path: Path) -> None:
    """Capture the visible desktop without relying on unsupported WebKit commands."""

    scrot = shutil.which("scrot")
    if sys.platform.startswith("linux") and scrot:
        completed = subprocess.run(
            [scrot, "--silent", "--overwrite", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise WebDriverError(
                f"scrot exited {completed.returncode}: {completed.stdout.strip()}"
            )
        return
    driver.screenshot(path)


def health_on_candidate_ports() -> tuple[int, dict[str, Any]] | None:
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


def wait_for_text(
    driver: WebDriver,
    text: str,
    *,
    timeout: float,
    source_path: Path | None = None,
) -> str:
    deadline = time.monotonic() + timeout
    last_source = ""
    while time.monotonic() < deadline:
        try:
            last_source = driver.source()
            if source_path is not None:
                source_path.write_text(last_source, encoding="utf-8", errors="replace")
            if text in last_source:
                return last_source
        except Exception:
            pass
        time.sleep(1)
    raise WebDriverError(f"Timed out waiting for {text!r}; last source: {last_source[:1000]}")


def collect_tauri_runtime_logs(evidence: EvidenceRun) -> list[Path]:
    collected: list[Path] = []
    candidates = [
        Path.home() / ".unsloth" / "studio" / "tauri.log",
        Path.home() / ".local" / "share" / "unsloth" / "tauri.log",
    ]
    for index, source in enumerate(candidates, start=1):
        if not source.is_file():
            continue
        destination = evidence.logs_dir / f"tauri-runtime-{index}.log"
        try:
            shutil.copy2(source, destination)
        except OSError:
            continue
        collected.append(destination)
    return collected


def cleanup_linux_desktop_backends() -> None:
    """Stop only disposable/CI backend processes rooted in the current home."""

    if not sys.platform.startswith("linux"):
        return
    home = str(Path.home().resolve())
    completed = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    targets: list[int] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        command = fields[1]
        if home not in command or " studio --api-only " not in f" {command} ":
            continue
        targets.append(pid)
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if health_on_candidate_ports() is None:
            return
        time.sleep(0.5)
    for pid in targets:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def first_install(args: argparse.Namespace) -> int:
    output = Path(args.evidence).resolve()
    evidence = EvidenceRun(output, f"{args.scenario}-tauri-first-install")
    evidence.begin(args.scenario)
    driver_log = evidence.logs_dir / "tauri-driver.log"
    driver_command = [args.tauri_driver, "--port", str(args.port)]
    if args.native_driver:
        driver_command.extend(["--native-driver", args.native_driver])
    with driver_log.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            driver_command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",
        )
    driver = WebDriver(f"http://127.0.0.1:{args.port}")
    phase_paths: list[Path] = [driver_log]
    try:
        driver.wait_ready(timeout=90)
        driver.start_session(Path(args.application))
        initial_source = output / "initial-source.html"
        wait_for_text(driver, "Get Started", timeout=180, source_path=initial_source)
        phase_paths.append(initial_source)
        initial_shot = evidence.screenshots_dir / "01-get-started.png"
        capture_desktop_screenshot(driver, initial_shot)
        phase_paths.append(initial_shot)

        button = driver.find_xpath("//button[normalize-space()='Get Started']")
        driver.click(button)
        installing_source = output / "installing-source.html"
        wait_for_text(driver, "Installing...", timeout=120, source_path=installing_source)
        phase_paths.append(installing_source)
        installing_shot = evidence.screenshots_dir / "02-installing.png"
        capture_desktop_screenshot(driver, installing_shot)
        phase_paths.append(installing_shot)

        deadline = time.monotonic() + args.install_timeout
        health: tuple[int, dict[str, Any]] | None = None
        last_source = ""
        while time.monotonic() < deadline:
            health = health_on_candidate_ports()
            try:
                last_source = driver.source()
                (output / "latest-source.html").write_text(
                    last_source, encoding="utf-8", errors="replace"
                )
            except Exception:
                pass
            if "Setup ran into a problem" in last_source or "Something went wrong" in last_source:
                raise WebDriverError(f"Desktop entered an error screen: {last_source[:2000]}")
            if health is not None:
                break
            time.sleep(5)
        if health is None:
            raise WebDriverError("No healthy Unsloth backend appeared on ports 8888-8908")

        port, payload = health
        health_path = output / "backend-health.json"
        health_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        phase_paths.append(health_path)
        running_shot = evidence.screenshots_dir / "03-running.png"
        capture_desktop_screenshot(driver, running_shot)
        phase_paths.append(running_shot)
        process_path = evidence.snapshot_processes("running")
        tree_path = evidence.snapshot_tree(
            "running",
            [
                Path.home() / ".unsloth",
                Path(args.application).parent,
            ],
        )
        phase_paths.extend([process_path, tree_path])

        if args.playwright_smoke:
            phase_paths.extend(
                run_installed_web_ui_smoke(evidence, args.playwright_smoke)
            )
        time.sleep(3)
        final_source = output / "final-source.html"
        settled_source = driver.source()
        final_source.write_text(
            settled_source, encoding="utf-8", errors="replace"
        )
        phase_paths.append(final_source)
        if "New chat" not in settled_source:
            raise WebDriverError(
                "Packaged desktop did not remain on its usable UI after backend "
                "and Playwright verification"
            )
        phase_paths.extend(collect_tauri_runtime_logs(evidence))

        evidence.record(
            args.scenario,
            "verified",
            f"Installed package launched, Get Started was clicked through WebDriver, "
            f"the owned API backend became healthy on port {port}, and the same "
            f"installed CLI's browser UI passed Playwright.",
            evidence=[path.relative_to(output) for path in phase_paths],
        )
        return 0
    except Exception as error:
        phase_paths.extend(collect_tauri_runtime_logs(evidence))
        try:
            failure_shot = evidence.screenshots_dir / "99-failure.png"
            capture_desktop_screenshot(driver, failure_shot)
            phase_paths.append(failure_shot)
        except Exception:
            pass
        phase_paths.extend(
            [
                evidence.snapshot_processes("failure"),
                evidence.snapshot_tree("failure", [Path.home() / ".unsloth"]),
            ]
        )
        evidence.record(
            args.scenario,
            "failed",
            f"Packaged first-launch flow failed: {error}",
            evidence=[
                path.relative_to(output) if path.is_relative_to(output) else path
                for path in phase_paths
            ],
            mismatch=str(error),
        )
        print(f"desktop lifecycle failure: {error}", file=sys.stderr)
        return 1
    finally:
        driver.close()
        if process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        cleanup_linux_desktop_backends()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--tauri-driver", default="tauri-driver")
    parser.add_argument("--native-driver")
    parser.add_argument("--port", type=int, default=4444)
    parser.add_argument("--install-timeout", type=float, default=7200)
    parser.add_argument("--playwright-smoke")
    args = parser.parse_args()
    return first_install(args)


if __name__ == "__main__":
    raise SystemExit(main())
