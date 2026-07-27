# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Destructive P0 probes; run only with an explicitly disposable HOME."""

from __future__ import annotations

import argparse
import os
import shlex
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


def concurrent_root_mutation(repo: Path, evidence: EvidenceRun, home: Path) -> None:
    """Barrier two copies of the installer's real replacement transaction."""

    scenario = "INST-07"
    evidence.begin(scenario)
    install_text = (repo / "install.sh").read_text(encoding="utf-8")
    block_start = install_text.index('_VENV_ROLLBACK_DIR=""')
    block_end_marker = "trap '_on_install_signal 143' TERM"
    block_end = install_text.index(block_end_marker, block_start) + len(block_end_marker)
    rollback_block = install_text[block_start:block_end]

    root = home / "concurrent-root" / "studio"
    venv = root / "unsloth_studio"
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "generation").write_text("original\n", encoding="utf-8")
    barrier = home / "concurrent-barrier"
    barrier.mkdir(parents=True, exist_ok=True)
    harnesses: list[Path] = []

    common = "\n".join(
        (
            "set -eu",
            "substep() { :; }",
            'C_WARN=""',
            f"STUDIO_HOME={shlex.quote(str(root))}",
            f"VENV_DIR={shlex.quote(str(venv))}",
            rollback_block,
        )
    )
    first = home / "installer-a.sh"
    write_executable(
        first,
        common
        + "\n"
        + "\n".join(
            (
                '_start_studio_venv_replacement "$VENV_DIR"',
                'mkdir -p "$VENV_DIR"',
                'printf "installer-a\\n" > "$VENV_DIR/generation"',
                f"touch {shlex.quote(str(barrier / 'a-ready'))}",
                f"for attempt in $(seq 1 600); do "
                f"[ -f {shlex.quote(str(barrier / 'b-ready'))} ] && break; sleep 0.05; done",
                f"test -f {shlex.quote(str(barrier / 'b-ready'))}",
                'printf "written-by-a-after-b-renamed-the-root\\n" >> '
                '"$VENV_DIR/mutations"',
                f"touch {shlex.quote(str(barrier / 'a-mutated'))}",
                f"for attempt in $(seq 1 600); do "
                f"[ -f {shlex.quote(str(barrier / 'b-done'))} ] && break; sleep 0.05; done",
                f"test -f {shlex.quote(str(barrier / 'b-done'))}",
                "_commit_studio_venv_replacement",
            )
        )
        + "\n",
    )
    second = home / "installer-b.sh"
    write_executable(
        second,
        common
        + "\n"
        + "\n".join(
            (
                '_start_studio_venv_replacement "$VENV_DIR"',
                'mkdir -p "$VENV_DIR"',
                'printf "installer-b\\n" > "$VENV_DIR/generation"',
                f"touch {shlex.quote(str(barrier / 'b-ready'))}",
                f"for attempt in $(seq 1 600); do "
                f"[ -f {shlex.quote(str(barrier / 'a-mutated'))} ] && break; sleep 0.05; done",
                f"test -f {shlex.quote(str(barrier / 'a-mutated'))}",
                'printf "written-by-b\\n" >> "$VENV_DIR/mutations"',
                "_commit_studio_venv_replacement",
                f"touch {shlex.quote(str(barrier / 'b-done'))}",
            )
        )
        + "\n",
    )
    harnesses.extend([first, second])
    coordinator = home / "concurrent-coordinator.sh"
    write_executable(
        coordinator,
        "\n".join(
            (
                "#!/bin/sh",
                "set -eu",
                f"sh {shlex.quote(str(first))} > "
                f"{shlex.quote(str(evidence.logs_dir / 'inst-07-installer-a.log'))} 2>&1 &",
                "first_pid=$!",
                f"for attempt in $(seq 1 600); do "
                f"[ -f {shlex.quote(str(barrier / 'a-ready'))} ] && break; sleep 0.05; done",
                f"test -f {shlex.quote(str(barrier / 'a-ready'))}",
                f"sh {shlex.quote(str(second))} > "
                f"{shlex.quote(str(evidence.logs_dir / 'inst-07-installer-b.log'))} 2>&1 &",
                "second_pid=$!",
                'wait "$first_pid"',
                'wait "$second_pid"',
                "",
            )
        ),
    )
    before = evidence.snapshot_tree("inst-07-before", [root])
    completed = evidence.run(
        ["sh", str(coordinator)],
        name="inst-07-concurrent-coordinator",
        cwd=repo,
        timeout=120,
        check=False,
    )
    after = evidence.snapshot_tree("inst-07-after", [root, barrier, *harnesses])
    generation = (venv / "generation").read_text(encoding="utf-8").strip()
    mutations_path = venv / "mutations"
    mutations = (
        mutations_path.read_text(encoding="utf-8").splitlines()
        if mutations_path.is_file()
        else []
    )
    cross_mutation = "written-by-a-after-b-renamed-the-root" in mutations
    evidence_paths = [
        before.relative_to(evidence.output_dir),
        after.relative_to(evidence.output_dir),
        Path("logs/inst-07-installer-a.log"),
        Path("logs/inst-07-installer-b.log"),
    ]
    if completed.returncode == 0 and generation == "installer-b" and cross_mutation:
        evidence.record(
            scenario,
            "failed",
            "Two live copies of the installer's exact replacement transaction "
            "mutated the same root concurrently.",
            evidence=evidence_paths,
            commands=[evidence.commands[-1].command],
            mismatch=(
                "Installer A continued writing through VENV_DIR after installer B "
                "had renamed A's environment and replaced that path; final generation "
                f"was {generation!r} with mutations {mutations!r}."
            ),
        )
    else:
        evidence.record(
            scenario,
            "not reproducible",
            "The deterministic concurrent-root mutation did not reproduce.",
            evidence=evidence_paths,
            commands=[evidence.commands[-1].command],
            limitation=(
                f"exit={completed.returncode}; generation={generation!r}; "
                f"mutations={mutations!r}"
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    home = assert_disposable_home()
    evidence = EvidenceRun(args.evidence, "p0-destructive-probes")
    concurrent_root_mutation(args.repo.resolve(), evidence, home)
    # Restore a clean root for the independent uninstall probe.
    full_uninstall(args.repo.resolve(), evidence, home)
    return 1 if any(result.status == "failed" for result in evidence.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
