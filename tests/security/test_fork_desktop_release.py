# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0
"""Fork desktop releases must resolve the actual backend pin and fork channel."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_pypi_verification_uses_backend_resolution_output():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-desktop.yml").read_text())
    prepare = workflow["jobs"]["prepare-version"]
    verify = next(
        step for step in prepare["steps"]
        if step.get("name") == "Verify PyPI package and Unsloth stamp"
    )
    assert verify["env"]["PYPI_VERSION"] == prepare["outputs"]["pypi_version"]


def test_fork_desktop_channel():
    config = json.loads((ROOT / "studio/src-tauri/tauri.conf.json").read_text())
    assert config["plugins"]["updater"]["endpoints"] == [
        "https://github.com/wasimysaid/unsloth/releases/latest/download/latest.json"
    ]
    for name in (
        ".github/workflows/release-desktop.yml",
        "studio/src-tauri/src/desktop_update_policy.rs",
        "studio/frontend/src/hooks/use-tauri-update.ts",
    ):
        source = (ROOT / name).read_text().split("#[cfg(test)]")[0]
        assert "unslothai/unsloth/releases" not in source
        assert "wasimysaid/unsloth/releases" in source
