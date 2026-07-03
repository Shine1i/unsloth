#!/usr/bin/env python3
"""Local small-model passthrough healing e2e for PR 6801."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx

LOCAL_MODEL = "unsloth/Qwen3-1.7B-GGUF"
LOCAL_GGUF_VARIANT = "UD-Q4_K_XL"
LOCAL_MAX_SEQ_LENGTH = 1024
LOCAL_PARALLEL = 1
FIXTURE = "<function=lookup><parameter=q>x</parameter></function>"
PROMPT = (
    "Output this literal XML snippet exactly, including the angle brackets. "
    "Do not output JSON. Do not omit the function or parameter tags. Snippet: "
    f"{FIXTURE}"
)
TOOL_SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
}


def pass_log(message: str) -> None:
    print(f"PASS {message}", flush=True)


def fail(message: str) -> None:
    print(f"FAIL {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_unsloth_bin(home: Path) -> Path:
    candidates = [
        home / "bin" / "unsloth",
        home / "bin" / "unsloth.exe",
        home / "unsloth_studio" / "bin" / "unsloth",
        home / "unsloth_studio" / "Scripts" / "unsloth.exe",
    ]
    candidates.extend(home.glob(".venv*/*/unsloth"))
    candidates.extend(home.glob(".venv*/Scripts/unsloth.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    fail(f"could not find unsloth CLI under {home}")


def wait_for_health(base_url: str, timeout_s: int = 900) -> str:
    deadline = time.time() + timeout_s
    for path in ("/healthz", "/api/health", "/"):
        pass
    while time.time() < deadline:
        for path in ("/healthz", "/api/health", "/"):
            try:
                with urllib.request.urlopen(f"{base_url}{path}", timeout=3) as resp:
                    if resp.status < 500:
                        return path
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
        time.sleep(2)
    fail("Studio run did not become healthy")


def read_api_key(log_path: Path, timeout_s: int = 900) -> str:
    deadline = time.time() + timeout_s
    pattern = re.compile(r"API Key:\s+(sk-unsloth-[a-f0-9]+)")
    while time.time() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
        match = pattern.search(text)
        if match:
            return match.group(1)
        time.sleep(2)
    fail("timed out waiting for Studio run API key")


def start_process(cmd: list[str], log_path: Path, env: dict[str, str]) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    kwargs = {"stdout": log_handle, "stderr": subprocess.STDOUT, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    print("Launching Studio run with small local model", flush=True)
    proc = subprocess.Popen(cmd, **kwargs)
    log_handle.close()
    return proc


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        proc.terminate()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()


def start_local_studio(home: Path, log_path: Path, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(home)
    env.pop("STUDIO_HOME", None)
    cmd = [
        str(find_unsloth_bin(home)),
        "studio",
        "run",
        "--model",
        LOCAL_MODEL,
        "--gguf-variant",
        LOCAL_GGUF_VARIANT,
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
        "--api-key-name",
        "ci",
        "--max-seq-length",
        str(LOCAL_MAX_SEQ_LENGTH),
        "--parallel",
        str(LOCAL_PARALLEL),
        "--disable-tools",
    ]
    return start_process(cmd, log_path, env)


def assert_lookup_tool_call(call: dict, label: str) -> None:
    fn = call.get("function") or {}
    name = fn.get("name") or call.get("name")
    if name != "lookup":
        fail(f"{label} returned wrong tool name: {name!r}")
    raw_args = fn.get("arguments", call.get("arguments", "{}"))
    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    if not isinstance(args, dict) or args.get("q") != "x":
        fail(f"{label} returned wrong arguments: {args!r}")


def chat_payload(*, stream: bool = False, tool_choice=None) -> dict:
    payload = {
        "model": LOCAL_MODEL,
        "messages": [
            {"role": "system", "content": "Follow the user's requested output exactly."},
            {"role": "user", "content": PROMPT},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "test lookup tool",
                    "parameters": TOOL_SCHEMA,
                },
            }
        ],
        "stream": stream,
        "max_tokens": 96,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return payload


def post_json(client: httpx.Client, base_url: str, api_key: str, path: str, payload: dict) -> dict:
    response = client.post(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=300,
    )
    if response.status_code >= 400:
        fail(f"{path} returned {response.status_code}: {response.text[:500]}")
    return response.json()


def check_chat_non_stream(client: httpx.Client, base_url: str, api_key: str, artifact_dir: Path) -> None:
    body = post_json(client, base_url, api_key, "/v1/chat/completions", chat_payload(),)
    (artifact_dir / "chat-non-stream.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
    msg = ((body.get("choices") or [{}])[0].get("message") or {})
    calls = msg.get("tool_calls") or []
    if not calls:
        fail(f"chat non-stream did not return tool_calls; content={msg.get('content')!r}")
    assert_lookup_tool_call(calls[0], "chat non-stream")
    if (body.get("choices") or [{}])[0].get("finish_reason") not in {"tool_calls", "stop"}:
        fail("chat non-stream returned unexpected finish_reason")
    pass_log("chat non-stream promoted text-form lookup call")


def check_chat_stream(client: httpx.Client, base_url: str, api_key: str, artifact_dir: Path) -> None:
    with client.stream(
        "POST",
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=chat_payload(stream=True),
        timeout=300,
    ) as response:
        if response.status_code >= 400:
            fail(f"chat stream returned {response.status_code}: {response.text[:500]}")
        lines = [line for line in response.iter_lines() if line]
    (artifact_dir / "chat-stream.sse").write_text("\n".join(lines), encoding="utf-8")
    tool_delta_seen = False
    for line in lines:
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        data = json.loads(line[6:])
        for choice in data.get("choices") or []:
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                if ((tc.get("function") or {}).get("name") == "lookup"):
                    tool_delta_seen = True
    if not tool_delta_seen:
        fail("chat stream did not emit a lookup tool_call delta")
    pass_log("chat stream emitted healed lookup tool_call delta")


def check_anthropic_non_stream(client: httpx.Client, base_url: str, api_key: str, artifact_dir: Path) -> None:
    payload = {
        "model": LOCAL_MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [{"name": "lookup", "description": "test lookup tool", "input_schema": TOOL_SCHEMA}],
        "max_tokens": 96,
        "temperature": 0,
        "auto_heal_tool_calls": True,
        "nudge_tool_calls": False,
    }
    body = post_json(client, base_url, api_key, "/v1/messages", payload)
    (artifact_dir / "anthropic-message.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
    tool_blocks = [block for block in body.get("content") or [] if block.get("type") == "tool_use"]
    if not tool_blocks:
        fail(f"anthropic non-stream did not return tool_use; content={body.get('content')!r}")
    block = tool_blocks[0]
    if block.get("name") != "lookup" or (block.get("input") or {}).get("q") != "x":
        fail(f"anthropic tool_use mismatch: {block!r}")
    pass_log("anthropic non-stream promoted text-form lookup tool_use")


def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifact_dir = Path(os.environ.get("STUDIO_ARTIFACT_DIR", "studio-local-tool-healing")).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = artifact_dir / "studio-run.log"
    proc = None
    try:
        proc = start_local_studio(home, log_path, port)
        api_key = read_api_key(log_path)
        health_path = wait_for_health(base_url)
        pass_log(f"Studio run healthy at {health_path}")
        with httpx.Client() as client:
            check_chat_non_stream(client, base_url, api_key, artifact_dir)
            check_chat_stream(client, base_url, api_key, artifact_dir)
            check_anthropic_non_stream(client, base_url, api_key, artifact_dir)
    finally:
        stop_process(proc)


if __name__ == "__main__":
    main()
