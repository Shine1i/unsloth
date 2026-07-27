# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Live Linux coexistence probes against an installed desktop package."""

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
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun  # noqa: E402
from linux_runtime_scenarios import (  # noqa: E402
    DriverSession,
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


DEFAULT_ROOT_ID = "a" * 64
CUSTOM_ROOT_ID = "b" * 64


def driver_args(
    args: argparse.Namespace, *, driver_port: int, native_port: int
) -> SimpleNamespace:
    return SimpleNamespace(
        application=args.application,
        tauri_driver=args.tauri_driver,
        driver_port=driver_port,
        native_port=native_port,
    )


def terminate_backend(pid: int, home: Path) -> None:
    command = pid_command(pid)
    if not command or str(home) not in command or " studio --api-only " not in f" {command} ":
        raise RuntimeError(f"Refusing to terminate unverified backend PID {pid}: {command}")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    wait_until(
        lambda: pid_command(pid) is None,
        30,
        f"disposable backend PID {pid} to stop",
    )


def stop_desktop(
    session: DriverSession,
    home: Path,
    *,
    preserve_backend_pid: int | None = None,
) -> None:
    session.stop()
    for pid in backend_pids(home):
        if pid == preserve_backend_pid:
            continue
        terminate_backend(pid, home)


def start_external_backend(
    cli: Path,
    log_path: Path,
    *,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], Any]:
    merged_environment = os.environ.copy()
    if environment:
        merged_environment.update(environment)
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(cli),
            "studio",
            "--api-only",
            "-H",
            "127.0.0.1",
            "-p",
            "8888",
        ],
        env=merged_environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    process._desktop_lifecycle_log_handle = log_handle  # type: ignore[attr-defined]
    try:
        health = wait_until(
            lambda: exact_health(8888),
            180,
            f"external backend from {cli}",
        )
    except Exception:
        stop_external_backend(process)
        raise
    return process, health


def stop_external_backend(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    log_handle = getattr(process, "_desktop_lifecycle_log_handle", None)
    if log_handle is not None:
        log_handle.close()


def capture_ui(
    driver: Any, evidence: EvidenceRun, label: str
) -> tuple[str, Path, Path, Path]:
    source = driver.source()
    source_path = evidence.output_dir / f"{label}-source.html"
    source_path.write_text(source, encoding="utf-8", errors="replace")
    screenshot = evidence.screenshots_dir / f"{label}.png"
    capture_desktop_screenshot(driver, screenshot)
    processes = evidence.snapshot_processes(label)
    return source, source_path, screenshot, processes


def health_after_8888() -> tuple[int, dict[str, Any]] | None:
    for port in range(8889, 8909):
        health = exact_health(port)
        if health is not None:
            return port, health
    return None


def remove_fixture_tree(path: Path, home: Path) -> None:
    if path.parent != home / ".unsloth":
        raise RuntimeError(f"Refusing to remove unexpected fixture path {path}")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


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
    default_root = home / ".unsloth" / "studio"
    backup_root = home / ".unsloth" / "desktop-lifecycle-default-root-backup"
    custom_root = home / ".unsloth" / "desktop-lifecycle-custom-root"
    if not default_root.is_dir():
        raise RuntimeError(f"Managed default root missing: {default_root}")
    if backup_root.exists() or custom_root.exists():
        raise RuntimeError("Refusing pre-existing coexistence fixture paths")

    cli = default_root / "unsloth_studio" / "bin" / "unsloth"
    if not cli.is_file():
        raise RuntimeError(f"Installed CLI missing: {cli}")

    os.environ.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
    evidence = EvidenceRun(args.evidence, "linux-coexistence")
    for scenario in (
        "COEX-04",
        "COEX-05",
        "COEX-06",
        "COEX-07",
        "COEX-08",
        "COEX-09",
    ):
        evidence.begin(scenario)
    manifest_path = evidence.output_dir / "coexistence-inputs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "application": str(args.application),
                "default_root": str(default_root),
                "custom_root": str(custom_root),
                "initial_studio_install_id_exists": (
                    default_root / "share" / "studio_install_id"
                ).is_file(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    common_paths = [manifest_path]
    sessions: list[DriverSession] = []
    externals: list[subprocess.Popen[str]] = []
    default_root_moved = False

    try:
        install_id = default_root / "share" / "studio_install_id"
        missing_id = not install_id.is_file()
        corrupt_session = DriverSession(
            driver_args(args, driver_port=4944, native_port=4945),
            evidence,
            "coex04-missing-install-id",
        )
        sessions.append(corrupt_session)
        corrupt_driver = corrupt_session.start()
        corrupt_port, corrupt_health = wait_until(
            health_on_candidate_ports,
            180,
            "backend from partial default environment",
        )
        wait_until(
            lambda: (
                source
                if any(
                    marker in source
                    for marker in ("New chat", "Something went wrong", "Get Started")
                )
                else None
            )
            if (source := corrupt_driver.source())
            else None,
            120,
            "settled UI from partial default environment",
        )
        corrupt_source, corrupt_source_path, corrupt_shot, corrupt_processes = capture_ui(
            corrupt_driver, evidence, "coex04-missing-install-id"
        )
        normal_ui = "New chat" in corrupt_source
        coex04_ok = not missing_id
        record(
            evidence,
            "COEX-04",
            coex04_ok,
            (
                "Launched the real partial default root with its install ID "
                f"missing={missing_id}; health root ID="
                f"{corrupt_health.get('studio_root_id')!r}, normal_ui={normal_ui}."
            ),
            [
                *common_paths,
                corrupt_source_path,
                corrupt_shot,
                corrupt_processes,
                evidence.logs_dir / "tauri-driver-coex04-missing-install-id.log",
            ],
            mismatch=(
                "The packaged install omitted share/studio_install_id but the "
                "desktop declared the environment ready instead of repairing it."
            ),
        )
        stop_desktop(corrupt_session, home)
        sessions.remove(corrupt_session)
        wait_until(
            lambda: exact_health(corrupt_port) is None,
            30,
            "COEX-04 backend cleanup",
        )

        default_root.rename(backup_root)
        default_root_moved = True
        custom_root.mkdir(parents=True)
        (custom_root / "share").mkdir()
        (custom_root / "share" / "studio_install_id").write_text(
            CUSTOM_ROOT_ID, encoding="utf-8"
        )
        (custom_root / "custom-root-canary").write_text(
            "desktop lifecycle custom root\n", encoding="utf-8"
        )
        (custom_root / "unsloth_studio").symlink_to(
            backup_root / "unsloth_studio", target_is_directory=True
        )
        custom_layout = evidence.output_dir / "coex05-custom-layout.json"
        custom_layout.write_text(
            json.dumps(
                {
                    "root": str(custom_root),
                    "venv_link": os.readlink(custom_root / "unsloth_studio"),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        custom_cli = custom_root / "unsloth_studio" / "bin" / "unsloth"
        custom_python = backup_root / "unsloth_studio" / "bin" / "python"
        readiness = evidence.run(
            [str(custom_python), str(custom_cli), "-h"],
            name="coex05-custom-root-readiness",
            env={"UNSLOTH_STUDIO_HOME": str(custom_root)},
            timeout=60,
            check=False,
        )
        custom_before = evidence.snapshot_tree(
            "coex05-custom-before",
            [
                custom_root / "share" / "studio_install_id",
                custom_root / "custom-root-canary",
            ],
        )
        old_custom_env = os.environ.get("UNSLOTH_STUDIO_HOME")
        os.environ["UNSLOTH_STUDIO_HOME"] = str(custom_root)
        custom_session = DriverSession(
            driver_args(args, driver_port=5044, native_port=5045),
            evidence,
            "coex05-custom-invisible",
        )
        sessions.append(custom_session)
        custom_driver = custom_session.start()
        custom_source = wait_until(
            lambda: (source if "Get Started" in source else None)
            if (source := custom_driver.source())
            else None,
            120,
            "Get Started with only a custom root present",
        )
        custom_source_path = evidence.output_dir / "coex05-custom-invisible-source.html"
        custom_source_path.write_text(
            custom_source, encoding="utf-8", errors="replace"
        )
        custom_shot = evidence.screenshots_dir / "coex05-custom-invisible.png"
        capture_desktop_screenshot(custom_driver, custom_shot)
        custom_processes = evidence.snapshot_processes("coex05-custom-invisible")
        custom_after = evidence.snapshot_tree(
            "coex05-custom-after",
            [
                custom_root / "share" / "studio_install_id",
                custom_root / "custom-root-canary",
            ],
        )
        generated_default = evidence.snapshot_tree(
            "coex05-generated-default-root",
            [default_root],
        )
        normalized_custom_source = custom_source.lower()
        custom_warning = any(
            marker in normalized_custom_source
            for marker in (
                "only the default",
                "custom-root install",
                "custom install",
                "default root",
            )
        )
        custom_unchanged = (
            (custom_root / "custom-root-canary").read_text(encoding="utf-8")
            == "desktop lifecycle custom root\n"
            and (custom_root / "share" / "studio_install_id").read_text(
                encoding="utf-8"
            )
            == CUSTOM_ROOT_ID
        )
        coex05_ok = readiness.returncode == 0 and custom_unchanged and custom_warning
        record(
            evidence,
            "COEX-05",
            coex05_ok,
            (
                "With only a ready custom root and its environment variable "
                f"present, desktop showed Get Started; warning={custom_warning}, "
                f"custom_unchanged={custom_unchanged}."
            ),
            [
                *common_paths,
                custom_layout,
                custom_before,
                custom_source_path,
                custom_shot,
                custom_processes,
                custom_after,
                generated_default,
                evidence.logs_dir / "coex05-custom-root-readiness.log",
                evidence.logs_dir / "tauri-driver-coex05-custom-invisible.log",
            ],
            mismatch=(
                "Desktop silently ignored the ready custom root and offered a "
                "second default-root install without disclosure or consent."
            ),
        )
        custom_session.stop()
        sessions.remove(custom_session)
        if old_custom_env is None:
            os.environ.pop("UNSLOTH_STUDIO_HOME", None)
        else:
            os.environ["UNSLOTH_STUDIO_HOME"] = old_custom_env
        if default_root.exists():
            if (default_root / "unsloth_studio").exists():
                raise RuntimeError(
                    "Desktop unexpectedly created a managed venv before root restore"
                )
            remove_fixture_tree(default_root, home)
        backup_root.rename(default_root)
        default_root_moved = False
        (custom_root / "unsloth_studio").unlink()
        (custom_root / "unsloth_studio").symlink_to(
            default_root / "unsloth_studio", target_is_directory=True
        )
        cli = default_root / "unsloth_studio" / "bin" / "unsloth"

        install_id.parent.mkdir(parents=True, exist_ok=True)
        install_id.write_text(DEFAULT_ROOT_ID, encoding="utf-8")

        old_custom_home = os.environ.get("UNSLOTH_STUDIO_HOME")
        old_studio_home = os.environ.get("STUDIO_HOME")
        old_path = os.environ["PATH"]
        os.environ["UNSLOTH_STUDIO_HOME"] = str(custom_root)
        os.environ["STUDIO_HOME"] = str(custom_root)
        os.environ["PATH"] = (
            f"{custom_root / 'unsloth_studio' / 'bin'}{os.pathsep}{old_path}"
        )
        default_wins_session = DriverSession(
            driver_args(args, driver_port=5094, native_port=5095),
            evidence,
            "coex06-07-default-wins",
        )
        sessions.append(default_wins_session)
        default_wins_driver = default_wins_session.start()
        default_wins_port, default_wins_health = wait_until(
            health_on_candidate_ports,
            180,
            "default-root backend with custom root and PATH CLI present",
        )
        default_wins_source = wait_until(
            lambda: (source if "New chat" in source else None)
            if (source := default_wins_driver.source())
            else None,
            120,
            "usable default-root UI with custom root and PATH CLI present",
        )
        (
            _source,
            default_wins_source_path,
            default_wins_shot,
            default_wins_processes,
        ) = capture_ui(default_wins_driver, evidence, "coex06-07-default-wins")
        default_wins_tree = evidence.snapshot_tree(
            "coex06-07-roots",
            [
                default_root / "share" / "studio_install_id",
                custom_root / "share" / "studio_install_id",
                custom_root / "custom-root-canary",
            ],
        )
        default_won = (
            default_wins_health.get("studio_root_id") == DEFAULT_ROOT_ID
            and "New chat" in _source
            and (custom_root / "share" / "studio_install_id").read_text(
                encoding="utf-8"
            )
            == CUSTOM_ROOT_ID
            and (custom_root / "custom-root-canary").read_text(encoding="utf-8")
            == "desktop lifecycle custom root\n"
        )
        common_default_wins_paths = [
            *common_paths,
            default_wins_source_path,
            default_wins_shot,
            default_wins_processes,
            default_wins_tree,
            evidence.logs_dir / "tauri-driver-coex06-07-default-wins.log",
        ]
        record(
            evidence,
            "COEX-06",
            default_won,
            (
                "With healthy custom and default roots plus both custom-root "
                f"variables exported, desktop started root ID "
                f"{default_wins_health.get('studio_root_id')!r} on "
                f"{default_wins_port}; custom state remained unchanged."
            ),
            common_default_wins_paths,
            mismatch=(
                "Desktop did not deterministically manage the default root or "
                "mutated the custom root."
            ),
        )
        record(
            evidence,
            "COEX-07",
            default_won,
            (
                "With the custom-root CLI first on PATH, desktop still started "
                f"the explicit default-root backend on {default_wins_port}; "
                "custom state remained unchanged."
            ),
            common_default_wins_paths,
            mismatch=(
                "Desktop followed the PATH CLI instead of its explicit "
                "default-root executable or mutated the other install."
            ),
        )
        stop_desktop(default_wins_session, home)
        sessions.remove(default_wins_session)
        if old_custom_home is None:
            os.environ.pop("UNSLOTH_STUDIO_HOME", None)
        else:
            os.environ["UNSLOTH_STUDIO_HOME"] = old_custom_home
        if old_studio_home is None:
            os.environ.pop("STUDIO_HOME", None)
        else:
            os.environ["STUDIO_HOME"] = old_studio_home
        os.environ["PATH"] = old_path

        same_log = evidence.logs_dir / "coex09-same-root-external.log"
        same_external, same_health = start_external_backend(cli, same_log)
        externals.append(same_external)
        same_session = DriverSession(
            driver_args(args, driver_port=5144, native_port=5145),
            evidence,
            "coex09-same-root-attach",
        )
        sessions.append(same_session)
        same_driver = same_session.start()
        same_source = wait_until(
            lambda: (source if "New chat" in source else None)
            if (source := same_driver.source())
            else None,
            120,
            "desktop attachment to same-root backend",
        )
        same_source_path = evidence.output_dir / "coex09-same-root-source.html"
        same_source_path.write_text(same_source, encoding="utf-8", errors="replace")
        same_shot = evidence.screenshots_dir / "coex09-same-root-attach.png"
        capture_desktop_screenshot(same_driver, same_shot)
        same_processes = evidence.snapshot_processes("coex09-same-root-attach")
        same_backend_only = backend_pids(home) == [same_external.pid]
        stop_desktop(same_session, home, preserve_backend_pid=same_external.pid)
        sessions.remove(same_session)
        external_survived = (
            same_external.poll() is None and exact_health(8888) is not None
        )
        coex09_ok = (
            same_health.get("studio_root_id") == DEFAULT_ROOT_ID
            and same_backend_only
            and external_survived
        )
        record(
            evidence,
            "COEX-09",
            coex09_ok,
            (
                "Desktop attached to the ownerless same-root terminal backend "
                f"PID {same_external.pid}; one_backend={same_backend_only}, "
                f"external_survived_desktop_exit={external_survived}."
            ),
            [
                *common_paths,
                same_log,
                same_source_path,
                same_shot,
                same_processes,
            ],
            mismatch=(
                f"health_root={same_health.get('studio_root_id')!r}; "
                f"backend_pids={backend_pids(home)}; external_survived={external_survived}"
            ),
        )
        stop_external_backend(same_external)
        externals.remove(same_external)

        custom_environment = {
            "UNSLOTH_STUDIO_HOME": str(custom_root),
            "STUDIO_HOME": str(custom_root),
        }
        foreign_log = evidence.logs_dir / "coex08-foreign-root-external.log"
        foreign_external, foreign_health = start_external_backend(
            cli,
            foreign_log,
            environment=custom_environment,
        )
        externals.append(foreign_external)
        foreign_session = DriverSession(
            driver_args(args, driver_port=5244, native_port=5245),
            evidence,
            "coex08-foreign-root",
        )
        sessions.append(foreign_session)
        foreign_driver = foreign_session.start()
        owned_port, owned_health = wait_until(
            health_after_8888,
            180,
            "default-root fallback backend beside foreign root",
        )
        foreign_source, foreign_source_path, foreign_shot, foreign_processes = capture_ui(
            foreign_driver, evidence, "coex08-foreign-root"
        )
        foreign_pid_set = backend_pids(home)
        own_pids = [pid for pid in foreign_pid_set if pid != foreign_external.pid]
        foreign_notice = "another" in foreign_source.lower() or "foreign" in foreign_source.lower()
        isolation_ok = (
            foreign_health.get("studio_root_id") == CUSTOM_ROOT_ID
            and owned_health.get("studio_root_id") == DEFAULT_ROOT_ID
            and len(own_pids) == 1
        )
        stop_desktop(
            foreign_session,
            home,
            preserve_backend_pid=foreign_external.pid,
        )
        sessions.remove(foreign_session)
        foreign_survived = (
            foreign_external.poll() is None and exact_health(8888) is not None
        )
        coex08_ok = isolation_ok and foreign_survived and foreign_notice
        observation_path = evidence.output_dir / "coex08-observations.json"
        observation_path.write_text(
            json.dumps(
                {
                    "foreign_backend_pid": foreign_external.pid,
                    "foreign_port": 8888,
                    "foreign_root_id": "<custom-root-id>",
                    "owned_backend_pids": own_pids,
                    "owned_port": owned_port,
                    "owned_root_id": "<default-root-id>",
                    "foreign_survived_desktop_exit": foreign_survived,
                    "ui_disclosed_foreign_root": foreign_notice,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        record(
            evidence,
            "COEX-08",
            coex08_ok,
            (
                f"Foreign root stayed on 8888 and desktop started default root "
                f"on {owned_port}; isolated={isolation_ok}, "
                f"foreign_survived={foreign_survived}, notice={foreign_notice}."
            ),
            [
                *common_paths,
                foreign_log,
                foreign_source_path,
                foreign_shot,
                foreign_processes,
                observation_path,
            ],
            mismatch=(
                "The two roots were process/port isolated, but desktop silently "
                "started a second backend without explaining the foreign active root."
            ),
        )
        stop_external_backend(foreign_external)
        externals.remove(foreign_external)
    except Exception as error:
        for scenario in (
            "COEX-04",
            "COEX-05",
            "COEX-06",
            "COEX-07",
            "COEX-08",
            "COEX-09",
        ):
            if not any(result.scenario == scenario for result in evidence.results):
                evidence.record(
                    scenario,
                    "failed",
                    f"Linux coexistence sequence failed before {scenario}: {error}",
                    evidence=[path.relative_to(evidence.output_dir) for path in common_paths],
                    mismatch=str(error),
                )
        print(f"coexistence failure: {error}", file=sys.stderr)
    finally:
        for session in reversed(sessions):
            try:
                stop_desktop(session, home)
            except Exception:
                session.stop()
        for process in reversed(externals):
            stop_external_backend(process)
        if default_root_moved:
            if default_root.exists():
                remove_fixture_tree(default_root, home)
            backup_root.rename(default_root)
        if custom_root.exists():
            remove_fixture_tree(custom_root, home)

    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
