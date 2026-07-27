# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Verify corrupt desktop owner-metadata recovery in a disposable Linux home."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun  # noqa: E402
from linux_runtime_scenarios import (  # noqa: E402
    DriverSession,
    assert_disposable_home,
    backend_pids,
    capture_owner,
    pid_command,
    wait_until,
)
from tauri_driver_flow import (  # noqa: E402
    capture_desktop_screenshot,
    health_on_candidate_ports,
)


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
    root = home / ".unsloth" / "studio"
    install_id = root / "share" / "studio_install_id"
    if not install_id.is_file():
        raise RuntimeError(
            "RUN-03 requires a valid-root fixture; share/studio_install_id is missing"
        )
    owner_path = root / "run" / "desktop_backend.json"
    owner_path.parent.mkdir(parents=True, exist_ok=True)
    preexisting_owner = owner_path.read_bytes() if owner_path.is_file() else None

    evidence = EvidenceRun(args.evidence, "linux-owner-metadata")
    evidence.begin("RUN-03")
    corrupt_input = evidence.output_dir / "owner-corrupt-input.json"
    corrupt_input.write_text('{"schema":1,"truncated":\n', encoding="utf-8")
    owner_path.write_text('{"schema":1,"truncated":', encoding="utf-8")
    session = DriverSession(
        SimpleNamespace(
            application=args.application,
            tauri_driver=args.tauri_driver,
            driver_port=5344,
            native_port=5345,
        ),
        evidence,
        "run03-corrupt-owner",
    )
    paths: list[Path] = [corrupt_input]

    try:
        driver = session.start()
        port, health = wait_until(
            health_on_candidate_ports,
            180,
            "managed backend after corrupt owner metadata",
        )
        owner_evidence = evidence.output_dir / "owner-recovered-redacted.json"
        owner = capture_owner(home, owner_evidence)
        source = wait_until(
            lambda: (value if "New chat" in value else None)
            if (value := driver.source())
            else None,
            120,
            "usable UI after corrupt owner metadata",
        )
        source_path = evidence.output_dir / "source-recovered.html"
        source_path.write_text(source, encoding="utf-8", errors="replace")
        screenshot = evidence.screenshots_dir / "run03-recovered.png"
        capture_desktop_screenshot(driver, screenshot)
        processes = evidence.snapshot_processes("run03-recovered")
        filesystem = evidence.snapshot_tree(
            "run03-recovered",
            [install_id, owner_path],
        )
        paths.extend(
            [
                owner_evidence,
                source_path,
                screenshot,
                processes,
                filesystem,
                evidence.logs_dir / "tauri-driver-run03-corrupt-owner.log",
            ]
        )
        recovered = (
            int(owner.get("port", 0)) == port
            and int(owner.get("backend_pid", 0)) in backend_pids(home)
            and health.get("studio_root_id")
            == install_id.read_text(encoding="utf-8")
        )
        evidence.record(
            "RUN-03",
            "verified" if recovered else "failed",
            (
                "Desktop replaced deliberately truncated owner metadata with "
                f"a live record for backend PID {owner.get('backend_pid')} on {port}."
            ),
            evidence=[path.relative_to(evidence.output_dir) for path in paths],
            mismatch=(
                None
                if recovered
                else f"owner={owner!r}; health={health!r}; pids={backend_pids(home)}"
            ),
        )
    except Exception as error:
        evidence.record(
            "RUN-03",
            "failed",
            f"Corrupt owner-metadata recovery failed: {error}",
            evidence=[
                path.relative_to(evidence.output_dir)
                for path in paths
                if path.exists()
            ],
            mismatch=str(error),
        )
        print(error, file=sys.stderr)
    finally:
        session.stop()
        for pid in backend_pids(home):
            command = pid_command(pid)
            if (
                command
                and str(home) in command
                and " studio --api-only " in f" {command} "
            ):
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if preexisting_owner is None:
            owner_path.unlink(missing_ok=True)
        else:
            owner_path.write_bytes(preexisting_owner)

    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
