# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Destructive P0 probes; run only with an explicitly disposable HOME."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvidenceRun  # noqa: E402


def assert_disposable_home() -> Path:
    home = Path.home().resolve()
    real_home = Path(os.environ.get("DESKTOP_LIFECYCLE_REAL_HOME", "/nonexistent")).resolve()
    marker = home / ".desktop-lifecycle-disposable"
    if not marker.is_file():
        raise RuntimeError(f"Refusing destructive probe: marker missing at {marker}")
    if home == real_home:
        raise RuntimeError(f"Refusing destructive probe in real HOME {home}")
    if len(home.parts) < 3:
        raise RuntimeError(f"Refusing suspiciously broad disposable HOME {home}")
    return home


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def bootstrap_integrity(repo: Path, evidence: EvidenceRun, home: Path) -> None:
    scenario = "INST-14"
    evidence.begin(scenario)
    shim_dir = home / "fault-bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    marker = home / "unverified-bootstrap-executed"
    write_executable(
        shim_dir / "uv",
        "#!/bin/sh\nprintf '%s\\n' 'uv 0.0.1'\n",
    )
    write_executable(
        shim_dir / "curl",
        """#!/bin/sh
set -eu
url=""
output=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then output="$argument"; previous=""; continue; fi
  if [ "$argument" = "-o" ]; then previous="-o"; continue; fi
  case "$argument" in http://*|https://*) url="$argument" ;; esac
done
if [ "$url" = "https://astral.sh/uv/install.sh" ]; then
  printf '%s\\n' '#!/bin/sh' 'touch "$HOME/unverified-bootstrap-executed"' 'exit 86' > "$output"
  exit 0
fi
exec /usr/bin/curl "$@"
""",
    )
    completed = evidence.run(
        ["bash", str(repo / "install.sh"), "--tauri", "--no-torch"],
        name="inst-14-substituted-bootstrap",
        cwd=repo,
        env={
            "HOME": str(home),
            "PATH": f"{shim_dir}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "UNSLOTH_SKIP_AUTOSTART": "1",
        },
        timeout=600,
        check=False,
    )
    tree = evidence.snapshot_tree("inst-14-after", [home / ".unsloth", marker, shim_dir])
    if marker.is_file():
        evidence.record(
            scenario,
            "failed",
            "The real installer downloaded and executed a substituted uv bootstrap "
            "without a pinned digest/signature check.",
            evidence=[tree.relative_to(evidence.output_dir)],
            commands=[evidence.commands[-1].command],
            mismatch=(
                "Expected fail-closed integrity verification before execution; "
                f"the injected bootstrap ran (installer exit {completed.returncode})."
            ),
        )
    else:
        evidence.record(
            scenario,
            "not reproducible",
            "The substituted uv bootstrap did not execute in this environment.",
            evidence=[tree.relative_to(evidence.output_dir)],
            commands=[evidence.commands[-1].command],
            limitation=f"installer exit={completed.returncode}",
        )


def full_uninstall(repo: Path, evidence: EvidenceRun, home: Path) -> None:
    scenario = "UN-02"
    evidence.begin(scenario)
    studio = home / ".unsloth" / "studio"
    categories = {
        "database": studio / "data" / "studio.db",
        "uploads": studio / "uploads" / "user-upload.txt",
        "outputs": studio / "outputs" / "trained-adapter.bin",
        "auth": studio / "auth" / "auth.db",
        "models": studio / "models" / "model-metadata.json",
        "rollback": studio / "unsloth_studio.rollback.audit" / "canary.txt",
    }
    owner = studio / "unsloth_studio" / ".unsloth-studio-owned"
    for path in [*categories.values(), owner]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"desktop lifecycle canary: {path.name}\n", encoding="utf-8")
    before = evidence.snapshot_tree("un-02-before", [studio])
    completed = evidence.run(
        ["sh", str(repo / "scripts" / "uninstall.sh")],
        name="un-02-full-uninstall",
        cwd=repo,
        env={"HOME": str(home)},
        timeout=120,
        check=False,
    )
    after = evidence.snapshot_tree("un-02-after", [studio, *categories.values()])
    deleted = sorted(name for name, path in categories.items() if not path.exists())
    if completed.returncode == 0 and len(deleted) == len(categories):
        evidence.record(
            scenario,
            "failed",
            "The documented full uninstaller reproduced deletion of every seeded "
            "Studio-local user-data category without a category-level choice.",
            evidence=[
                before.relative_to(evidence.output_dir),
                after.relative_to(evidence.output_dir),
            ],
            commands=[evidence.commands[-1].command],
            mismatch="Deleted categories: " + ", ".join(deleted),
        )
    else:
        evidence.record(
            scenario,
            "not reproducible",
            "The full-uninstall data-loss behavior did not reproduce completely.",
            evidence=[
                before.relative_to(evidence.output_dir),
                after.relative_to(evidence.output_dir),
            ],
            commands=[evidence.commands[-1].command],
            limitation=(
                f"exit={completed.returncode}; deleted={deleted}; "
                f"remaining={[name for name, path in categories.items() if path.exists()]}"
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    home = assert_disposable_home()
    evidence = EvidenceRun(args.evidence, "p0-destructive-probes")
    bootstrap_integrity(args.repo.resolve(), evidence, home)
    # Restore a clean root for the independent uninstall probe.
    full_uninstall(args.repo.resolve(), evidence, home)
    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
