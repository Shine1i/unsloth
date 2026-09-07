# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0


"""Package-relative assets for Studio's bundled Blender integration."""

import sys
from pathlib import Path

BUNDLE_PATH = Path(__file__).resolve().parents[2] / "vendor" / "blender_mcp"
MIN_PYTHON_VERSION = (3, 10)
MIN_BLENDER_VERSION = "5.1.0"


def launch_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve().with_name("launcher.py"))]
