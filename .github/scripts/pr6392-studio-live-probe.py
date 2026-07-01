#!/usr/bin/env python3
"""Live Studio API probe for PR #6392.

Installs/launches Studio through the generated workflow, then verifies the
OpenAI auto-switch settings and model listing API against a real running server.
Do not print passwords, access tokens, refresh tokens, or API keys.
"""

from __future__ import annotations

import asyncio
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
from studio_test_kit.auth import login


def pass_log(message: str) -> None:
    print(f"PASS {message}", flush=True)


def warn(message: str) -> None:
    print(f"WARN {message}", flush=True)


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


def read_bootstrap_password(home: Path, log_path: Path) -> str | None:
    for rel in ("auth/.bootstrap_password", ".bootstrap_password"):
        path = home / rel
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(
        r"(?i)(?:bootstrap|initial|generated)\s*password(?:\s+is)?\s*[:=]?\s+(\S+)",
        log_text,
    )
    return match.group(1).strip().strip(".,") if match else None


def wait_for_health(base_url: str, timeout_s: int = 240) -> str:
    deadline = time.time() + timeout_s
    paths = ("/api/health", "/healthz", "/")
    while time.time() < deadline:
        for path in paths:
            try:
                with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as resp:
                    if resp.status < 500:
                        return path
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
        time.sleep(2)
    fail(f"Studio did not become healthy within {timeout_s}s")


def start_studio(home: Path, artifact_dir: Path, port: int) -> subprocess.Popen:
    log_path = artifact_dir / "studio.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(home)
    env.pop("STUDIO_HOME", None)
    cmd = [str(find_unsloth_bin(home)), "studio", "-H", "127.0.0.1", "-p", str(port), "--no-cloudflare"]
    print("Launching Studio", flush=True)
    log_handle = log_path.open("w", encoding="utf-8")
    kwargs: dict = {"stdout": log_handle, "stderr": subprocess.STDOUT, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
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
        proc.kill()


async def change_password_if_needed(base_url: str, auth, password: str) -> str:
    if not getattr(auth, "must_change_password", False):
        return auth.access_token
    new_password = os.environ.get("STUDIO_TEST_PASSWORD", "UnslothStudioCI2026!")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base_url}/api/auth/change-password",
            headers={"Authorization": f"Bearer {auth.access_token}"},
            json={"current_password": password, "new_password": new_password},
        )
        response.raise_for_status()
        body = response.json()
    pass_log("first-boot password change completed")
    return body["access_token"]


async def authed_json(client: httpx.AsyncClient, method: str, url: str, token: str, **kwargs):
    response = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    return response


async def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifact_dir = Path(os.environ.get("STUDIO_ARTIFACT_DIR", "studio-live-artifacts")).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = start_studio(home, artifact_dir, port)
    temp_key_id = None
    try:
        health_path = wait_for_health(base_url)
        pass_log(f"Studio healthy at {health_path}")
        password = read_bootstrap_password(home, artifact_dir / "studio.log")
        if not password:
            fail("could not read bootstrap password")
        auth = await login(base_url, "unsloth", password)
        token = await change_password_if_needed(base_url, auth, password)
        pass_log("Studio API login succeeded")
        async with httpx.AsyncClient(timeout=60) as client:
            create = await authed_json(
                client,
                "POST",
                f"{base_url}/api/auth/api-keys",
                token,
                json={"name": "pr6392-live-probe", "expires_in_days": 1},
            )
            create.raise_for_status()
            key_body = create.json()
            api_key = key_body["key"]
            temp_key_id = key_body["api_key"]["id"]
            api_headers = {"Authorization": f"Bearer {api_key}"}

            unauth = await client.get(f"{base_url}/v1/models")
            if unauth.status_code != 401:
                fail(f"/v1/models without auth returned {unauth.status_code}, expected 401")
            pass_log("/v1/models rejects unauthenticated requests")

            get1 = await client.get(f"{base_url}/api/settings/openai-auto-switch", headers=api_headers)
            get1.raise_for_status()
            initial = get1.json()
            put = await client.put(
                f"{base_url}/api/settings/openai-auto-switch",
                headers=api_headers,
                json={"enabled": True, "auto_unload_idle_seconds": 0},
            )
            put.raise_for_status()
            enabled = put.json()
            if enabled.get("enabled") is not True or enabled.get("auto_unload_idle_seconds") != 0:
                fail(f"settings PUT did not round-trip enabled state: {enabled}")
            pass_log("openai-auto-switch settings PUT enabled the feature")

            models = await client.get(f"{base_url}/v1/models", headers=api_headers)
            models.raise_for_status()
            model_body = models.json()
            if model_body.get("object") != "list" or not isinstance(model_body.get("data"), list):
                fail(f"unexpected /v1/models body: {model_body}")
            pass_log(f"/v1/models returned list with {len(model_body.get('data') or [])} entries")

            restore = await client.put(
                f"{base_url}/api/settings/openai-auto-switch",
                headers=api_headers,
                json={
                    "enabled": bool(initial.get("enabled", False)),
                    "auto_unload_idle_seconds": int(initial.get("auto_unload_idle_seconds") or 0),
                },
            )
            restore.raise_for_status()
            pass_log("openai-auto-switch settings restored")

            if temp_key_id is not None:
                revoke = await authed_json(
                    client,
                    "DELETE",
                    f"{base_url}/api/auth/api-keys/{temp_key_id}",
                    token,
                )
                revoke.raise_for_status()
                pass_log("temporary API key revoked")
                temp_key_id = None
    finally:
        stop_process(proc)


if __name__ == "__main__":
    asyncio.run(main())
