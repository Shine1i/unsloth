#!/usr/bin/env python3
"""Targeted evidence for PR #6581's AnyIO comment rationale."""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
import traceback
from importlib import metadata
from pathlib import Path

import anyio
import starlette
from starlette.responses import StreamingResponse


ERROR_TEXT = "Attempted to exit a cancel scope that isn't the current tasks's current cancel scope"


def dist_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def assert_source_shape(expected_anyio: str) -> bool:
    from anyio._backends import _asyncio as asyncio_backend
    from anyio._core import _tasks

    has_task_handle = hasattr(_tasks, "TaskHandle")
    backend_source = Path(asyncio_backend.__file__).read_text()

    if expected_anyio == "4.13.0":
        if has_task_handle:
            raise AssertionError("AnyIO 4.13 unexpectedly exposes TaskHandle")
        if "handle._run_coro()" in backend_source:
            raise AssertionError("AnyIO 4.13 unexpectedly wraps spawned tasks in TaskHandle._run_coro()")
        print("PASS_SOURCE anyio=4.13.0 TaskHandle absent and no _run_coro wrapper")
        return False

    if expected_anyio == "4.14.0":
        if not has_task_handle:
            raise AssertionError("AnyIO 4.14 does not expose TaskHandle")
        task_handle_source = Path(_tasks.__file__).read_text()
        required = [
            ".. versionadded:: 4.14.0",
            "self._cancel_scope = CancelScope()",
            "async def _run_coro",
            "with self._cancel_scope:",
        ]
        missing = [needle for needle in required if needle not in task_handle_source]
        if missing:
            raise AssertionError(f"AnyIO 4.14 TaskHandle source missing expected strings: {missing}")
        if "handle._run_coro()" not in backend_source:
            raise AssertionError("AnyIO 4.14 asyncio backend does not spawn handle._run_coro()")
        print("PASS_SOURCE anyio=4.14.0 TaskHandle/_run_coro per-task CancelScope present")
        return True

    raise AssertionError(f"unexpected expected_anyio={expected_anyio!r}")


async def run_streaming_stress(expected_anyio: str, iterations: int) -> bool:
    async def exercise_once(index: int) -> None:
        async def content():
            for chunk in range(100):
                await anyio.sleep(0)
                yield f"data: {index}:{chunk}\\n\\n".encode()

        async def receive():
            await anyio.sleep(0 if index % 2 else 0.0001)
            return {"type": "http.disconnect"}

        async def send(message):
            return None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        }
        response = StreamingResponse(content(), media_type="text/event-stream")
        await response(scope, receive, send)

    for index in range(iterations):
        try:
            await exercise_once(index)
        except RuntimeError as exc:
            if ERROR_TEXT in str(exc):
                print(f"RUNTIME_REPRO anyio={expected_anyio} iteration={index} error={exc}")
                if expected_anyio == "4.13.0":
                    raise
                return True
            raise
        except BaseException:
            traceback.print_exc()
            raise

    print(f"RUNTIME_NO_REPRO anyio={expected_anyio} iterations={iterations}")
    return False


async def run_synthetic_taskhandle_guard(expected_anyio: str, has_task_handle: bool) -> None:
    if not has_task_handle:
        print(f"SYNTHETIC_SKIP anyio={expected_anyio} reason=no_TaskHandle")
        return

    from anyio._backends import _asyncio as asyncio_backend
    from anyio._core._tasks import TaskHandle

    handle = None

    async def payload() -> None:
        task = asyncio.current_task()
        state = asyncio_backend._task_states[task]
        if state.cancel_scope is not handle._cancel_scope:
            raise AssertionError("TaskHandle cancel scope was not current inside _run_coro")
        state.cancel_scope = None

    handle = TaskHandle(payload(), "pr6581-synthetic-mismatch")
    task = asyncio.create_task(handle._run_coro())
    try:
        await task
    except RuntimeError as exc:
        if ERROR_TEXT in str(exc):
            print(f"SYNTHETIC_REPRO anyio={expected_anyio} error={exc}")
            return
        raise

    raise AssertionError("Synthetic TaskHandle cancel-scope mismatch did not raise")


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anyio", required=True, choices=["4.13.0", "4.14.0"])
    parser.add_argument("--iterations", type=int, default=300)
    args = parser.parse_args()

    print(f"RUNNER_OS={os.environ.get('RUNNER_OS', 'local')}")
    print(f"platform={platform.platform()} machine={platform.machine()} processor={platform.processor()}")
    print(f"python={sys.version.split()[0]} executable={sys.executable}")
    print(f"anyio={dist_version('anyio')} starlette={starlette.__version__} fastapi={dist_version('fastapi')}")

    if dist_version("anyio") != args.anyio:
        raise AssertionError(f"installed AnyIO {dist_version('anyio')} != expected {args.anyio}")

    has_task_handle = assert_source_shape(args.anyio)
    runtime_repro = await run_streaming_stress(args.anyio, args.iterations)
    await run_synthetic_taskhandle_guard(args.anyio, has_task_handle)

    if args.anyio == "4.13.0" and runtime_repro:
        raise AssertionError("AnyIO 4.13 reproduced the 4.14-only RuntimeError")

    print(f"PASS anyio={args.anyio} runtime_repro={runtime_repro}")


if __name__ == "__main__":
    asyncio.run(amain())
