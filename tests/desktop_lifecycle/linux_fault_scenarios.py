# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Live Linux loader and read-only-root fault probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun  # noqa: E402
from linux_runtime_scenarios import (  # noqa: E402
    DriverSession,
    assert_disposable_home,
    record,
    wait_until,
)
from tauri_driver_flow import capture_desktop_screenshot  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--webkit-link", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tauri-driver", default="tauri-driver")
    args = parser.parse_args()
    args.application = args.application.resolve()
    args.webkit_link = args.webkit_link.absolute()

    home = assert_disposable_home()
    if args.application != Path("/usr/bin/unsloth-studio"):
        raise RuntimeError(f"Refusing unexpected application {args.application}")
    if not args.webkit_link.is_symlink():
        raise RuntimeError(f"Expected WebKit runtime symlink: {args.webkit_link}")
    if "libwebkit2gtk-4.1.so.0" not in args.webkit_link.name:
        raise RuntimeError(f"Refusing unexpected runtime library {args.webkit_link}")

    default_root = home / ".unsloth" / "studio"
    backup_root = home / ".unsloth" / "desktop-lifecycle-fault-root-backup"
    held_library = args.webkit_link.with_name(
        args.webkit_link.name + ".desktop-lifecycle-held"
    )
    if not default_root.is_dir() or backup_root.exists() or held_library.exists():
        raise RuntimeError("Fault fixture preconditions were not clean")

    evidence = EvidenceRun(args.evidence, "linux-loader-and-filesystem-faults")
    for scenario in ("PKG-06", "INST-10"):
        evidence.begin(scenario)
    manifest = evidence.output_dir / "fault-inputs.json"
    manifest.write_text(
        json.dumps(
            {
                "application": str(args.application),
                "webkit_link": str(args.webkit_link),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    common_paths = [manifest]
    session: DriverSession | None = None
    root_moved = False
    library_held = False

    canary = default_root / "outputs" / "package-transition-canary"
    canary_hash = sha256(canary) if canary.is_file() else None
    try:
        hold = evidence.run(
            ["sudo", "mv", str(args.webkit_link), str(held_library)],
            name="pkg06-hold-webkit-runtime-link",
            timeout=30,
            check=False,
        )
        library_held = hold.returncode == 0
        launch = evidence.run(
            [str(args.application)],
            name="pkg06-launch-without-webkit-runtime",
            timeout=30,
            check=False,
        )
        restore = evidence.run(
            ["sudo", "mv", str(held_library), str(args.webkit_link)],
            name="pkg06-restore-webkit-runtime-link",
            timeout=30,
            check=False,
        )
        library_held = restore.returncode != 0
        no_home_mutation = (
            canary_hash is None
            or (canary.is_file() and sha256(canary) == canary_hash)
        )
        loader_rejected = (
            launch.returncode != 0
            and "libwebkit2gtk-4.1.so.0" in launch.stdout
        )
        # A desktop-entry launch has no terminal in which to show ld.so's text.
        actionable_native_ui = False
        record(
            evidence,
            "PKG-06",
            loader_rejected and no_home_mutation and actionable_native_ui,
            (
                "With libwebkit2gtk-4.1.so.0 withheld, the installed app exited "
                f"{launch.returncode}; loader_rejected={loader_rejected}, "
                f"home_unchanged={no_home_mutation}, native_error_ui="
                f"{actionable_native_ui}."
            ),
            [
                *common_paths,
                evidence.logs_dir / "pkg06-hold-webkit-runtime-link.log",
                evidence.logs_dir / "pkg06-launch-without-webkit-runtime.log",
                evidence.logs_dir / "pkg06-restore-webkit-runtime-link.log",
            ],
            mismatch=(
                "The loader safely rejected the launch before managed-state "
                "mutation, but a desktop-entry user receives no native actionable UI."
            ),
        )

        default_root.rename(backup_root)
        root_moved = True
        default_root.mkdir()
        default_root.chmod(0o555)
        driver_namespace = SimpleNamespace(
            application=args.application,
            tauri_driver=args.tauri_driver,
            driver_port=5344,
            native_port=5345,
        )
        session = DriverSession(
            driver_namespace,
            evidence,
            "inst10-read-only-root",
        )
        driver = session.start()
        initial_source = wait_until(
            lambda: (source if "Get Started" in source else None)
            if (source := driver.source())
            else None,
            120,
            "Get Started on read-only root",
        )
        initial_source_path = evidence.output_dir / "inst10-initial-source.html"
        initial_source_path.write_text(
            initial_source, encoding="utf-8", errors="replace"
        )
        driver.click(driver.find_xpath("//button[normalize-space()='Get Started']"))
        error_source = wait_until(
            lambda: (
                source
                if "Setup ran into a problem" in source
                or "Permission needed" in source
                else None
            )
            if (source := driver.source())
            else None,
            180,
            "read-only root setup disposition",
        )
        error_source_path = evidence.output_dir / "inst10-error-source.html"
        error_source_path.write_text(
            error_source, encoding="utf-8", errors="replace"
        )
        error_shot = evidence.screenshots_dir / "inst10-read-only-root.png"
        capture_desktop_screenshot(driver, error_shot)
        processes = evidence.snapshot_processes("inst10-read-only-root")
        generated_tree = evidence.snapshot_tree(
            "inst10-read-only-root", [default_root]
        )
        actionable = any(
            marker in error_source.lower()
            for marker in ("permission denied", "read-only", str(default_root).lower())
        )
        empty_elevation_prompt = (
            "Permission needed" in error_source
            and "The following system packages need to be installed:" in error_source
            and "Setup ran into a problem" not in error_source
        )
        backup_preserved = (
            canary_hash is None
            or (
                (backup_root / "outputs" / "package-transition-canary").is_file()
                and sha256(backup_root / "outputs" / "package-transition-canary")
                == canary_hash
            )
        )
        no_cli_created = not (
            default_root / "unsloth_studio" / "bin" / "unsloth"
        ).exists()
        record(
            evidence,
            "INST-10",
            (
                actionable
                and not empty_elevation_prompt
                and backup_preserved
                and no_cli_created
            ),
            (
                "The actual setup settled after EACCES against a mode-0555 "
                f"default root; empty_elevation_prompt={empty_elevation_prompt}, "
                f"actionable_path_error={actionable}, "
                f"prior_data_preserved={backup_preserved}, "
                f"no_cli_created={no_cli_created}."
            ),
            [
                *common_paths,
                initial_source_path,
                error_source_path,
                error_shot,
                processes,
                generated_tree,
                evidence.logs_dir / "tauri-driver-inst10-read-only-root.log",
            ],
            mismatch=(
                "The installer returned 2 for uv EACCES; Tauri interpreted that "
                "as missing apt packages and rendered an empty Permission needed "
                "prompt instead of the read-only path error."
            ),
        )
    except Exception as error:
        for scenario in ("PKG-06", "INST-10"):
            if not any(result.scenario == scenario for result in evidence.results):
                evidence.record(
                    scenario,
                    "failed",
                    f"Linux fault sequence failed before {scenario}: {error}",
                    evidence=[path.relative_to(evidence.output_dir) for path in common_paths],
                    mismatch=str(error),
                )
        print(f"fault scenario failure: {error}", file=sys.stderr)
    finally:
        if session is not None:
            session.stop()
        if library_held and held_library.exists():
            subprocess.run(
                ["sudo", "mv", str(held_library), str(args.webkit_link)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        if root_moved:
            if default_root.exists():
                default_root.chmod(0o700)
                shutil.rmtree(default_root)
            backup_root.rename(default_root)

    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
