#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Start a real `unsloth run` server portably for PR #7103 disposable CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def request_json(url: str, token: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=10
    ) as response:
        return json.load(response)


def emit(name: str, value: str) -> None:
    print(f"{name}={value}")
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as file:
            file.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-file", required=True)
    parser.add_argument("--port", type=int, default=18971)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--log", default="logs/unsloth-run.log")
    args = parser.parse_args()

    model_file = str(Path(args.model_file).resolve())
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "unsloth",
        "run",
        "-H",
        "127.0.0.1",
        "-p",
        str(args.port),
        "--disable-tools",
        "--no-cloudflare",
        "--model",
        model_file,
        "--seed",
        "3407",
        "--temp",
        "0",
    ]
    print("Launching:", " ".join(command))
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )

    base = f"http://127.0.0.1:{args.port}"
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            if request_json(f"{base}/api/health").get("status") == "healthy":
                break
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(2)
    else:
        process.terminate()
        print(f"Server did not become healthy in {args.timeout}s", file=sys.stderr)
        print(log_path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
        return 1

    if process.poll() is not None:
        print(f"Server exited with {process.returncode}", file=sys.stderr)
        print(log_path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
        return 1

    token = ""
    for _ in range(60):
        text = log_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"sk-unsloth-[A-Za-z0-9_-]+", text)
        if match:
            token = match.group(0)
            break
        time.sleep(1)
    if not token:
        print("Could not parse the API key from the server banner", file=sys.stderr)
        return 1

    print(f"::add-mask::{token}")
    try:
        models = request_json(f"{base}/v1/models", token)
        model_id = models["data"][0]["id"]
    except (KeyError, IndexError, OSError, ValueError, urllib.error.URLError) as error:
        print(f"Could not resolve the loaded model: {error}", file=sys.stderr)
        return 1

    emit("UNSLOTH_API_KEY", token)
    emit("UNSLOTH_STUDIO_URL", base)
    emit("UNSLOTH_BASE_URL", base)
    emit("UNSLOTH_MODEL_ID", str(model_id))
    emit("UNSLOTH_SERVER_PID", str(process.pid))
    print(f"Server ready on {base}; model={model_id}; pid={process.pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
