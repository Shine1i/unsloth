# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Live Linux runtime lifecycle probes for an installed Tauri package.

Run only inside a disposable home and X11 session. The probe occupies port
8888 with a real unrelated HTTP server, launches the installed desktop app,
crashes only processes it resolves inside the disposable home, and records
fallback, adoption, watchdog, and retry behavior.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun  # noqa: E402
from tauri_driver_flow import (  # noqa: E402
    WebDriver,
    capture_desktop_screenshot,
    health_on_candidate_ports,
)


def assert_disposable_home() -> Path:
    home = Path.home().resolve()
    marker = home / ".desktop-lifecycle-disposable"
    if not marker.is_file():
        raise RuntimeError(f"Refusing runtime probe: marker missing at {marker}")
    if len(home.parts) < 3:
        raise RuntimeError(f"Refusing suspiciously broad disposable HOME {home}")
    return home


def process_rows() -> list[tuple[int, str]]:
    completed = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    rows: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        rows.append((int(fields[0]), fields[1]))
    return rows


def application_pids(application: Path) -> list[int]:
    pids: list[int] = []
    for pid, command in process_rows():
        executable = command.split(maxsplit=1)[0]
        try:
            matches = Path(executable).resolve() == application.resolve()
        except OSError:
            matches = executable == str(application)
        if matches:
            pids.append(pid)
    return pids


def backend_pids(home: Path) -> list[int]:
    marker = str(home)
    return [
        pid
        for pid, command in process_rows()
        if marker in command and " studio --api-only " in f" {command} "
    ]


def pid_command(pid: int) -> str | None:
    for candidate, command in process_rows():
        if candidate == pid:
            return command
    return None


def wait_until(predicate, timeout: float, description: str) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as error:
            last = error
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {description}; last={last!r}")


def exact_health(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=2
        ) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if (
            payload.get("status") == "healthy"
            and payload.get("service") == "Unsloth UI Backend"
        ):
            return payload
    except Exception:
        return None
    return None


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if any(marker in key.lower() for marker in ("token", "secret", "password"))
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def capture_owner(
    home: Path, destination: Path, *, timeout: float = 60
) -> dict[str, Any]:
    owner_path = home / ".unsloth" / "studio" / "run" / "desktop_backend.json"

    def ready_owner() -> dict[str, Any] | None:
        if not owner_path.is_file():
            return None
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        if int(owner.get("backend_pid", 0)) <= 0 or not isinstance(
            owner.get("port"), int
        ):
            return None
        return owner

    owner = wait_until(
        ready_owner,
        timeout,
        f"desktop owner metadata at {owner_path}",
    )
    destination.write_text(
        json.dumps(redact(owner), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return owner


class DriverSession:
    def __init__(self, args: argparse.Namespace, evidence: EvidenceRun, label: str) -> None:
        self.args = args
        self.evidence = evidence
        self.label = label
        self.driver = WebDriver(f"http://127.0.0.1:{args.driver_port}")
        self.process: subprocess.Popen[str] | None = None
        self.log_handle = None

    def start(self) -> WebDriver:
        command = [
            self.args.tauri_driver,
            "--port",
            str(self.args.driver_port),
            "--native-port",
            str(self.args.native_port),
        ]
        self.log_handle = (self.evidence.logs_dir / f"tauri-driver-{self.label}.log").open(
            "w", encoding="utf-8"
        )
        environment = os.environ.copy()
        environment.setdefault("GDK_BACKEND", "x11")
        environment.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
        environment.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        self.process = subprocess.Popen(
            command,
            env=environment,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.driver.wait_ready(timeout=90)
        self.driver.start_session(self.args.application)
        return self.driver

    def stop(self) -> None:
        self.driver.close()
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=10)
        self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None


def record(
    evidence: EvidenceRun,
    scenario: str,
    passed: bool,
    summary: str,
    paths: Iterable[Path],
    *,
    mismatch: str | None = None,
) -> None:
    evidence.record(
        scenario,
        "verified" if passed else "failed",
        summary,
        evidence=[path.relative_to(evidence.output_dir) for path in paths],
        mismatch=None if passed else mismatch,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tauri-driver", default="tauri-driver")
    parser.add_argument("--driver-port", type=int, default=4544)
    parser.add_argument("--native-port", type=int, default=4545)
    args = parser.parse_args()
    args.application = args.application.resolve()

    home = assert_disposable_home()
    evidence = EvidenceRun(args.evidence, "linux-runtime-lifecycle")
    for scenario in ("RUN-01", "RUN-02", "RUN-04", "RUN-10"):
        evidence.begin(scenario)

    command_manifest = evidence.output_dir / "commands.json"
    command_manifest.write_text(
        json.dumps(
            {
                "unrelated_listener": [
                    sys.executable,
                    "-m",
                    "http.server",
                    "8888",
                    "--bind",
                    "127.0.0.1",
                ],
                "desktop_application": str(args.application),
                "tauri_driver": [
                    args.tauri_driver,
                    "--port",
                    str(args.driver_port),
                    "--native-port",
                    str(args.native_port),
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    listener_log = evidence.logs_dir / "unrelated-listener.log"
    listener_handle = listener_log.open("w", encoding="utf-8")
    listener = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            "8888",
            "--bind",
            "127.0.0.1",
        ],
        stdout=listener_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    sessions: list[DriverSession] = []
    owned_pids: set[int] = set()
    common_paths = [command_manifest, listener_log]
    try:
        wait_until(
            lambda: listener.poll() is None
            and urllib.request.urlopen("http://127.0.0.1:8888/", timeout=2).status
            == 200,
            30,
            "unrelated HTTP listener on port 8888",
        )
        first_session = DriverSession(args, evidence, "initial")
        sessions.append(first_session)
        driver = first_session.start()
        backend = wait_until(
            health_on_candidate_ports, 180, "desktop backend on candidate ports"
        )
        backend_port, backend_health = backend
        backend_pid = wait_until(
            lambda: backend_pids(home), 30, "installed desktop backend process"
        )[0]
        owned_pids.add(backend_pid)
        initial_source_path = evidence.output_dir / "source-initial.html"
        initial_source_path.write_text(driver.source(), encoding="utf-8", errors="replace")
        initial_shot = evidence.screenshots_dir / "runtime-initial.png"
        capture_desktop_screenshot(driver, initial_shot)
        initial_processes = evidence.snapshot_processes("runtime-initial")
        initial_owner_path = evidence.output_dir / "owner-initial-redacted.json"
        initial_owner: dict[str, Any] | None
        try:
            initial_owner = capture_owner(home, initial_owner_path, timeout=5)
        except Exception as owner_error:
            initial_owner = None
            initial_owner_path.write_text(
                json.dumps(
                    {
                        "metadata": "missing",
                        "expected_path": str(
                            home
                            / ".unsloth"
                            / "studio"
                            / "run"
                            / "desktop_backend.json"
                        ),
                        "health_studio_root_id": backend_health.get("studio_root_id"),
                        "observed_backend_pid": backend_pid,
                        "error": str(owner_error),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        initial_paths = [
            *common_paths,
            initial_owner_path,
            initial_source_path,
            initial_shot,
            initial_processes,
        ]

        fallback_ok = (
            backend_port != 8888
            and backend_health.get("service") == "Unsloth UI Backend"
            and listener.poll() is None
        )
        record(
            evidence,
            "RUN-01",
            fallback_ok,
            (
                f"Desktop backend started healthy on fallback port {backend_port} "
                f"while port 8888 was occupied."
            ),
            initial_paths,
            mismatch=f"backend_port={backend_port}; listener_exit={listener.poll()}",
        )
        record(
            evidence,
            "RUN-02",
            fallback_ok,
            "The unrelated HTTP listener on port 8888 remained alive and was not adopted or killed.",
            initial_paths,
            mismatch=f"backend_port={backend_port}; listener_exit={listener.poll()}",
        )
        if not fallback_ok:
            raise RuntimeError("Fallback baseline failed; later runtime cases are invalid")

        if initial_owner is None:
            record(
                evidence,
                "RUN-04",
                False,
                (
                    "The installed Tauri flow did not create owner metadata, so "
                    "a crashed shell cannot prove or perform backend adoption."
                ),
                initial_paths,
                mismatch=(
                    "desktop_backend.json missing after healthy startup; "
                    f"health.studio_root_id={backend_health.get('studio_root_id')!r}"
                ),
            )
        else:
            app_pid = wait_until(
                lambda: application_pids(args.application),
                30,
                f"installed application process {args.application}",
            )[0]
            os.kill(app_pid, signal.SIGKILL)
            wait_until(
                lambda: pid_command(app_pid) is None,
                30,
                f"application PID {app_pid} to exit after SIGKILL",
            )
            if exact_health(backend_port) is None:
                raise RuntimeError("Backend did not survive the simulated app crash")
            first_session.stop()

            crashed_owner_path = evidence.output_dir / "owner-after-app-crash-redacted.json"
            crashed_owner = capture_owner(home, crashed_owner_path)
            second_session = DriverSession(args, evidence, "adoption")
            sessions.append(second_session)
            driver = second_session.start()
            adopted_health = wait_until(
                lambda: exact_health(backend_port), 120, "adopted backend health"
            )
            adopted_owner_path = evidence.output_dir / "owner-adopted-redacted.json"
            adopted_owner = capture_owner(home, adopted_owner_path)
            adopted_source_path = evidence.output_dir / "source-adopted.html"
            adopted_source_path.write_text(
                driver.source(), encoding="utf-8", errors="replace"
            )
            adopted_shot = evidence.screenshots_dir / "runtime-adopted.png"
            capture_desktop_screenshot(driver, adopted_shot)
            adopted_processes = evidence.snapshot_processes("runtime-adopted")
            adoption_ok = (
                int(crashed_owner["backend_pid"]) == backend_pid
                and int(adopted_owner["backend_pid"]) == backend_pid
                and int(adopted_owner["port"]) == backend_port
                and adopted_health.get("status") == "healthy"
            )
            adoption_paths = [
                *common_paths,
                initial_owner_path,
                crashed_owner_path,
                adopted_owner_path,
                adopted_source_path,
                adopted_shot,
                adopted_processes,
            ]
            record(
                evidence,
                "RUN-04",
                adoption_ok,
                (
                    f"After SIGKILL of app PID {app_pid}, relaunch adopted the "
                    f"same healthy backend PID {backend_pid} on port {backend_port}."
                ),
                adoption_paths,
                mismatch=(
                    f"initial={initial_owner!r}; crashed={crashed_owner!r}; "
                    f"adopted={adopted_owner!r}"
                ),
            )
            if not adoption_ok:
                raise RuntimeError("Backend adoption invariant failed")

        os.kill(backend_pid, signal.SIGKILL)
        error_source = wait_until(
            lambda: (
                source
                if any(
                    text in source
                    for text in (
                        "Something went wrong",
                        "Setup ran into a problem",
                        "Retry",
                    )
                )
                else None
            )
            if (source := driver.source())
            else None,
            120,
            "watchdog error UI after backend SIGKILL",
        )
        error_source_path = evidence.output_dir / "source-backend-crashed.html"
        error_source_path.write_text(error_source, encoding="utf-8", errors="replace")
        error_shot = evidence.screenshots_dir / "runtime-backend-crashed.png"
        capture_desktop_screenshot(driver, error_shot)
        retry_button = driver.find_xpath("//button[normalize-space()='Retry']")
        driver.click(retry_button)
        restarted = wait_until(
            health_on_candidate_ports, 180, "healthy backend after Retry"
        )
        restarted_port, restarted_health = restarted
        restarted_owner_path = evidence.output_dir / "owner-restarted-redacted.json"
        restarted_owner_error: str | None = None
        try:
            restarted_owner = capture_owner(home, restarted_owner_path, timeout=5)
        except Exception as owner_error:
            restarted_owner = None
            restarted_owner_error = str(owner_error)
        restarted_pid = wait_until(
            lambda: [
                pid
                for pid in backend_pids(home)
                if pid != backend_pid and pid_command(pid) is not None
            ],
            30,
            "replacement desktop backend process",
        )[0]
        if restarted_owner is None:
            restarted_owner_path.write_text(
                json.dumps(
                    {
                        "metadata": "missing",
                        "observed_backend_pid": restarted_pid,
                        "observed_port": restarted_port,
                        "error": restarted_owner_error,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        owned_pids.add(restarted_pid)
        recovered_source_path = evidence.output_dir / "source-recovered.html"
        recovered_source_path.write_text(driver.source(), encoding="utf-8", errors="replace")
        recovered_shot = evidence.screenshots_dir / "runtime-recovered.png"
        capture_desktop_screenshot(driver, recovered_shot)
        recovered_processes = evidence.snapshot_processes("runtime-recovered")
        recovery_ok = (
            restarted_pid != backend_pid
            and restarted_port == backend_port
            and restarted_health.get("status") == "healthy"
            and listener.poll() is None
        )
        recovery_paths = [
            *common_paths,
            error_source_path,
            error_shot,
            restarted_owner_path,
            recovered_source_path,
            recovered_shot,
            recovered_processes,
        ]
        record(
            evidence,
            "RUN-10",
            recovery_ok,
            (
                f"Watchdog surfaced a Retry UI after backend PID {backend_pid} "
                f"was killed; Retry started healthy PID {restarted_pid} on "
                f"fallback port {restarted_port}."
            ),
            recovery_paths,
            mismatch=(
                f"old_pid={backend_pid}; restarted={restarted_owner!r}; "
                f"listener_exit={listener.poll()}"
            ),
        )
    except Exception as error:
        for scenario in ("RUN-01", "RUN-02", "RUN-04", "RUN-10"):
            if not any(result.scenario == scenario for result in evidence.results):
                evidence.record(
                    scenario,
                    "failed",
                    f"Runtime lifecycle sequence failed before {scenario}: {error}",
                    evidence=[path.relative_to(evidence.output_dir) for path in common_paths],
                    mismatch=str(error),
                )
        print(f"runtime lifecycle failure: {error}", file=sys.stderr)
    finally:
        for session in reversed(sessions):
            session.stop()
        for pid in owned_pids:
            command = pid_command(pid)
            if command and str(home) in command:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if listener.poll() is None:
            os.killpg(listener.pid, signal.SIGTERM)
            try:
                listener.wait(timeout=10)
            except subprocess.TimeoutExpired:
                listener.kill()
        listener_handle.close()

    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
