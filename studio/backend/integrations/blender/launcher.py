# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0


"""Launch the bundled official Blender MCP server over stdio only."""

import sys
from pathlib import Path


def main() -> int:
    if sys.version_info < (3, 10):
        print("Bundled Blender MCP requires Python 3.10 or newer.", file = sys.stderr)
        return 1
    if len(sys.argv) != 1:
        print("The bundled Blender MCP launcher accepts no arguments.", file = sys.stderr)
        return 2
    vendor = Path(__file__).resolve().parents[2] / "vendor" / "blender_mcp" / "mcp"
    sys.path.insert(0, str(vendor))
    from blmcp import main as upstream_main

    sys.argv = [str(Path(__file__).resolve()), "--transport", "stdio"]
    return upstream_main()


if __name__ == "__main__":
    raise SystemExit(main())
