#!/usr/bin/env python3
"""Create a compact X11 work area and assert the live Tauri window fits it."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path


def output(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def create_dock() -> None:
    from Xlib import X, Xatom, display

    connection = display.Display()
    screen = connection.screen()
    root = screen.root
    dock = root.create_window(
        0,
        460,
        1000,
        40,
        0,
        screen.root_depth,
        X.InputOutput,
        X.CopyFromParent,
        background_pixel=screen.black_pixel,
        event_mask=X.StructureNotifyMask,
    )
    dock.change_property(
        connection.intern_atom("_NET_WM_WINDOW_TYPE"),
        Xatom.ATOM,
        32,
        [connection.intern_atom("_NET_WM_WINDOW_TYPE_DOCK")],
    )
    dock.change_property(
        connection.intern_atom("_NET_WM_STRUT_PARTIAL"),
        Xatom.CARDINAL,
        32,
        [0, 0, 0, 40, 0, 0, 0, 0, 0, 999, 0, 0],
    )
    dock.change_property(
        connection.intern_atom("_NET_WM_STRUT"),
        Xatom.CARDINAL,
        32,
        [0, 0, 0, 40],
    )
    dock.map()
    connection.sync()
    print(f"PASS dock mapped id=0x{dock.id:x}", flush=True)
    while True:
        connection.sync()
        time.sleep(1)


def parse_work_area() -> tuple[int, int, int, int]:
    raw = output("xprop", "-root", "_NET_WORKAREA")
    values = [int(value) for value in re.findall(r"-?\d+", raw.split("=", 1)[-1])]
    if len(values) < 4:
        raise RuntimeError(f"cannot parse _NET_WORKAREA: {raw.strip()}")
    return tuple(values[:4])  # type: ignore[return-value]


def find_unsloth_window() -> str | None:
    for line in output("wmctrl", "-l").splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[3].strip() == "Unsloth":
            return parts[0]
    return None


def parse_window(window_id: str) -> dict[str, int]:
    raw = output("xwininfo", "-id", window_id)

    def field(label: str) -> int:
        match = re.search(rf"{re.escape(label)}:\s*(-?\d+)", raw)
        if not match:
            raise RuntimeError(f"missing {label!r} in xwininfo:\n{raw}")
        return int(match.group(1))

    frame = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    try:
        extents = output("xprop", "-id", window_id, "_NET_FRAME_EXTENTS")
        values = [int(value) for value in re.findall(r"-?\d+", extents.split("=", 1)[-1])]
        if len(values) >= 4:
            frame = dict(zip(frame, values[:4], strict=True))
    except subprocess.CalledProcessError:
        pass

    client_x = field("Absolute upper-left X")
    client_y = field("Absolute upper-left Y")
    client_width = field("Width")
    client_height = field("Height")
    return {
        "x": client_x - frame["left"],
        "y": client_y - frame["top"],
        "width": client_width + frame["left"] + frame["right"],
        "height": client_height + frame["top"] + frame["bottom"],
        "client_width": client_width,
        "client_height": client_height,
        **{f"frame_{key}": value for key, value in frame.items()},
    }


def assert_geometry() -> None:
    deadline = time.monotonic() + 300
    window_id = None
    while time.monotonic() < deadline:
        window_id = find_unsloth_window()
        if window_id:
            break
        time.sleep(1)
    if not window_id:
        raise RuntimeError("Unsloth window did not appear within 300 seconds")

    work_x, work_y, work_width, work_height = parse_work_area()
    if (work_x, work_y, work_width, work_height) != (0, 0, 1000, 460):
        raise AssertionError(
            f"FAIL expected work area 0,0 1000x460, got "
            f"{work_x},{work_y} {work_width}x{work_height}"
        )

    geometry = parse_window(window_id)
    geometry["work_x"] = work_x
    geometry["work_y"] = work_y
    geometry["work_width"] = work_width
    geometry["work_height"] = work_height
    Path("logs").mkdir(exist_ok=True)
    Path("logs/linux-geometry.json").write_text(json.dumps(geometry, indent=2) + "\n")

    right = geometry["x"] + geometry["width"]
    bottom = geometry["y"] + geometry["height"]
    if not (
        geometry["x"] >= work_x
        and geometry["y"] >= work_y
        and right <= work_x + work_width
        and bottom <= work_y + work_height
    ):
        raise AssertionError(f"FAIL window escapes work area: {geometry}")
    print(
        "PASS live Linux window fits work area: "
        f"outer={geometry['width']}x{geometry['height']}@{geometry['x']},{geometry['y']} "
        f"work={work_width}x{work_height}@{work_x},{work_y}",
        flush=True,
    )


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"dock", "assert"}:
        raise SystemExit("usage: pr8047_linux_geometry.py dock|assert")
    if sys.argv[1] == "dock":
        create_dock()
    else:
        assert_geometry()


if __name__ == "__main__":
    main()
