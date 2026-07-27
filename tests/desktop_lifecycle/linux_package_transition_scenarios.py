# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Exercise Linux native-uninstall and package-format transitions.

This probe is intentionally destructive and refuses to run outside a marked
disposable home. It removes and reinstalls the exact local deb while the real
packaged app is active, then launches the deb and AppImage in both orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun  # noqa: E402
from linux_runtime_scenarios import (  # noqa: E402
    DriverSession,
    application_pids,
    assert_disposable_home,
    backend_pids,
    exact_health,
    pid_command,
    record,
    wait_until,
)
from tauri_driver_flow import (  # noqa: E402
    capture_desktop_screenshot,
    health_on_candidate_ports,
)


EXPECTED_PACKAGE = "unsloth-studio-desktop"
EXPECTED_DEB_APPLICATION = Path("/usr/bin/unsloth-studio")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def driver_args(
    args: argparse.Namespace, application: Path, driver_port: int, native_port: int
) -> SimpleNamespace:
    return SimpleNamespace(
        application=application.resolve(),
        tauri_driver=args.tauri_driver,
        driver_port=driver_port,
        native_port=native_port,
    )


def start_primary(
    args: argparse.Namespace,
    evidence: EvidenceRun,
    *,
    application: Path,
    label: str,
    driver_port: int,
    native_port: int,
) -> tuple[DriverSession, Any, int, dict[str, Any]]:
    session = DriverSession(
        driver_args(args, application, driver_port, native_port), evidence, label
    )
    driver = session.start()
    port, health = wait_until(
        health_on_candidate_ports, 180, f"{label} desktop backend health"
    )
    return session, driver, port, health


def stop_session(session: DriverSession, backend_port: int, home: Path) -> None:
    session.stop()
    try:
        wait_until(
            lambda: exact_health(backend_port) is None,
            20,
            f"backend on port {backend_port} to stop",
        )
    except RuntimeError:
        cleanup_disposable_backends(home)
        wait_until(
            lambda: exact_health(backend_port) is None,
            30,
            f"manually cleaned disposable backend on port {backend_port} to stop",
        )


def launch_secondary(application: Path, log_path: Path) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
    environment.setdefault("GDK_BACKEND", "x11")
    environment.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    environment.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(application)],
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    process._desktop_lifecycle_log_handle = log_handle  # type: ignore[attr-defined]
    return process


def observe_secondary(process: subprocess.Popen[str], timeout: float = 45) -> int | None:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.5)
    return process.poll()


def stop_secondary(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    log_handle = getattr(process, "_desktop_lifecycle_log_handle", None)
    if log_handle is not None:
        log_handle.close()


def package_switch_notice(source: str) -> bool:
    normalized = source.lower()
    return any(
        marker in normalized
        for marker in (
            "another package format",
            "appimage and .deb",
            "deb and appimage",
            "package migration",
        )
    )


def cleanup_disposable_backends(home: Path) -> None:
    for pid in backend_pids(home):
        command = pid_command(pid)
        if not command or str(home) not in command:
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--appimage", type=Path, required=True)
    parser.add_argument("--application", type=Path, default=EXPECTED_DEB_APPLICATION)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tauri-driver", default="tauri-driver")
    args = parser.parse_args()
    args.deb = args.deb.resolve()
    args.appimage = args.appimage.resolve()
    args.application = args.application.resolve()

    home = assert_disposable_home()
    if args.application != EXPECTED_DEB_APPLICATION:
        raise RuntimeError(f"Refusing unexpected deb application {args.application}")
    for artifact in (args.deb, args.appimage):
        if not artifact.is_file() or home not in artifact.parents:
            raise RuntimeError(f"Refusing artifact outside disposable home: {artifact}")
    if not os.access(args.appimage, os.X_OK):
        raise RuntimeError(f"AppImage is not executable: {args.appimage}")

    package = subprocess.run(
        ["dpkg-deb", "-f", str(args.deb), "Package"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    ).stdout.strip()
    if package != EXPECTED_PACKAGE:
        raise RuntimeError(f"Refusing unexpected deb package {package!r}")

    os.environ.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
    evidence = EvidenceRun(args.evidence, "linux-package-transitions")
    for scenario in ("UN-01", "UN-03", "UN-04", "UN-06"):
        evidence.begin(scenario)

    manifest_path = evidence.output_dir / "package-inputs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "package": package,
                "deb": str(args.deb),
                "appimage": str(args.appimage),
                "deb_application": str(args.application),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    sentinel = home / ".unsloth" / "studio" / "outputs" / "package-transition-canary"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        "desktop-lifecycle package transition canary\n", encoding="utf-8"
    )
    sentinel_hash = sha256(sentinel)
    sessions: list[tuple[DriverSession, int]] = []
    secondaries: list[subprocess.Popen[str]] = []
    common_paths = [manifest_path]

    try:
        session, driver, backend_port, _ = start_primary(
            args,
            evidence,
            application=args.application,
            label="deb-active-uninstall",
            driver_port=4644,
            native_port=4645,
        )
        sessions.append((session, backend_port))
        before_shot = evidence.screenshots_dir / "un03-before-uninstall.png"
        capture_desktop_screenshot(driver, before_shot)
        before_processes = evidence.snapshot_processes("un03-before-uninstall")
        app_pid = application_pids(args.application)[0]
        backend_pid = backend_pids(home)[0]

        removal = evidence.run(
            ["sudo", "dpkg", "-r", package],
            name="un03-dpkg-remove-while-active",
            timeout=180,
            check=False,
        )
        time.sleep(5)
        after_processes = evidence.snapshot_processes("un03-after-uninstall")
        after_tree = evidence.snapshot_tree(
            "un03-after-uninstall",
            [
                args.application,
                sentinel,
                home / ".unsloth" / "studio" / "studio.db",
            ],
        )
        app_survived = pid_command(app_pid) is not None
        backend_survived = exact_health(backend_port) is not None
        package_removed = (
            removal.returncode == 0
            and not args.application.exists()
            and sentinel.is_file()
            and sha256(sentinel) == sentinel_hash
        )
        un03_ok = package_removed and not app_survived and not backend_survived
        record(
            evidence,
            "UN-03",
            un03_ok,
            (
                f"dpkg removed {package} while Tauri PID {app_pid} and backend "
                f"PID {backend_pid} were active; app_survived={app_survived}, "
                f"backend_survived={backend_survived}."
            ),
            [
                *common_paths,
                before_shot,
                before_processes,
                after_processes,
                after_tree,
                evidence.logs_dir / "un03-dpkg-remove-while-active.log",
            ],
            mismatch=(
                f"package_removed={package_removed}; app_survived={app_survived}; "
                f"backend_survived={backend_survived}"
            ),
        )

        stop_session(session, backend_port, home)
        sessions.remove((session, backend_port))
        reinstall = evidence.run(
            ["sudo", "dpkg", "-i", str(args.deb)],
            name="reinstall-deb-after-active-removal",
            timeout=180,
            check=False,
        )
        preservation_ok = (
            reinstall.returncode == 0
            and args.application.is_file()
            and sentinel.is_file()
            and sha256(sentinel) == sentinel_hash
        )
        reinstall_tree = evidence.snapshot_tree(
            "reinstalled-deb",
            [args.application, sentinel, home / ".unsloth" / "studio" / "studio.db"],
        )
        preservation_paths = [
            *common_paths,
            reinstall_tree,
            evidence.logs_dir / "reinstall-deb-after-active-removal.log",
        ]
        record(
            evidence,
            "UN-01",
            preservation_ok,
            "Native deb removal and reinstall preserved the seeded managed-data canary.",
            preservation_paths,
            mismatch=(
                f"reinstall_exit={reinstall.returncode}; app_exists="
                f"{args.application.exists()}; sentinel_hash_ok="
                f"{sentinel.is_file() and sha256(sentinel) == sentinel_hash}"
            ),
        )
        record(
            evidence,
            "UN-06",
            preservation_ok,
            "The exact deb reinstalled successfully over preserved managed state.",
            preservation_paths,
            mismatch=f"reinstall_exit={reinstall.returncode}",
        )
        if not preservation_ok:
            raise RuntimeError("Could not restore exact deb after UN-03")

        directions: list[dict[str, Any]] = []

        deb_session, deb_driver, deb_port, _ = start_primary(
            args,
            evidence,
            application=args.application,
            label="un04-deb-primary",
            driver_port=4744,
            native_port=4745,
        )
        sessions.append((deb_session, deb_port))
        appimage_secondary_log = evidence.logs_dir / "un04-appimage-secondary.log"
        appimage_secondary = launch_secondary(args.appimage, appimage_secondary_log)
        secondaries.append(appimage_secondary)
        appimage_exit = observe_secondary(appimage_secondary)
        deb_source = deb_driver.source()
        deb_source_path = evidence.output_dir / "un04-deb-primary-source.html"
        deb_source_path.write_text(deb_source, encoding="utf-8", errors="replace")
        deb_shot = evidence.screenshots_dir / "un04-deb-primary.png"
        capture_desktop_screenshot(deb_driver, deb_shot)
        deb_snapshot = evidence.snapshot_processes("un04-deb-then-appimage")
        directions.append(
            {
                "primary": "deb",
                "secondary": "AppImage",
                "secondary_exit": appimage_exit,
                "package_notice": package_switch_notice(deb_source),
                "deb_app_pids": application_pids(args.application),
                "backend_pids": backend_pids(home),
            }
        )
        stop_secondary(appimage_secondary)
        secondaries.remove(appimage_secondary)
        stop_session(deb_session, deb_port, home)
        sessions.remove((deb_session, deb_port))

        appimage_session, appimage_driver, appimage_port, _ = start_primary(
            args,
            evidence,
            application=args.appimage,
            label="un04-appimage-primary",
            driver_port=4844,
            native_port=4845,
        )
        sessions.append((appimage_session, appimage_port))
        deb_secondary_log = evidence.logs_dir / "un04-deb-secondary.log"
        deb_secondary = launch_secondary(args.application, deb_secondary_log)
        secondaries.append(deb_secondary)
        deb_exit = observe_secondary(deb_secondary)
        appimage_source = appimage_driver.source()
        appimage_source_path = evidence.output_dir / "un04-appimage-primary-source.html"
        appimage_source_path.write_text(
            appimage_source, encoding="utf-8", errors="replace"
        )
        appimage_shot = evidence.screenshots_dir / "un04-appimage-primary.png"
        capture_desktop_screenshot(appimage_driver, appimage_shot)
        appimage_snapshot = evidence.snapshot_processes("un04-appimage-then-deb")
        directions.append(
            {
                "primary": "AppImage",
                "secondary": "deb",
                "secondary_exit": deb_exit,
                "package_notice": package_switch_notice(appimage_source),
                "deb_app_pids": application_pids(args.application),
                "backend_pids": backend_pids(home),
            }
        )
        switch_observations = evidence.output_dir / "un04-observations.json"
        switch_observations.write_text(
            json.dumps(directions, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        package_notice = all(item["package_notice"] for item in directions)
        stale_launcher_retired = not args.appimage.exists() or not args.application.exists()
        un04_ok = package_notice and stale_launcher_retired
        record(
            evidence,
            "UN-04",
            un04_ok,
            (
                "Launched the real deb and AppImage in both orders; "
                f"package_notice={package_notice}, "
                f"stale_launcher_retired={stale_launcher_retired}."
            ),
            [
                *common_paths,
                deb_source_path,
                deb_shot,
                deb_snapshot,
                appimage_source_path,
                appimage_shot,
                appimage_snapshot,
                appimage_secondary_log,
                deb_secondary_log,
                switch_observations,
            ],
            mismatch=(
                f"directions={directions!r}; appimage_exists={args.appimage.exists()}; "
                f"deb_exists={args.application.exists()}"
            ),
        )
    except Exception as error:
        for scenario in ("UN-01", "UN-03", "UN-04", "UN-06"):
            if not any(result.scenario == scenario for result in evidence.results):
                evidence.record(
                    scenario,
                    "failed",
                    f"Linux package transition sequence failed before {scenario}: {error}",
                    evidence=[path.relative_to(evidence.output_dir) for path in common_paths],
                    mismatch=str(error),
                )
        print(f"package transition failure: {error}", file=sys.stderr)
    finally:
        for process in reversed(secondaries):
            stop_secondary(process)
        for session, port in reversed(sessions):
            try:
                stop_session(session, port, home)
            except Exception:
                session.stop()
        cleanup_disposable_backends(home)
        if not args.application.is_file():
            restore = subprocess.run(
                ["sudo", "dpkg", "-i", str(args.deb)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
                check=False,
            )
            (evidence.logs_dir / "cleanup-restore-deb.log").write_text(
                restore.stdout, encoding="utf-8", errors="replace"
            )

    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
