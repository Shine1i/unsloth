#!/usr/bin/env python3
"""Secret-free live Studio lifecycle probe for PR #10443.

Runs only against an already installed, isolated UNSLOTH_STUDIO_HOME. The
result artifact contains assertions and public implementation pins, never auth
credentials or request headers.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

EXPECTED_PR_HEAD = "bcd9a8a25c9fe509c9220528c79bf4022a20410c"
REVISION = "4309a39646e644261624bfcd2bca669b343b7621"
ARCHIVE_SHA256 = "acb68eb4beff27a84ba751931745e62f03ad51b7be50b3a924624153b6c38197"
PORT = int(os.environ.get("STUDIO_PORT", "18943"))
BASE = f"http://127.0.0.1:{PORT}"
HOME = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
OUT = Path(os.environ.get("EVIDENCE_DIR", "evidence")).resolve()
LOG = OUT / "studio-raw.log"
RESULT = OUT / "result.json"
SAFE_LOG = OUT / "lifecycle.log"

passes: list[str] = []
facts: dict[str, Any] = {
    "schema": 1,
    "upstream_pr": 10443,
    "expected_pr_head": EXPECTED_PR_HEAD,
    "os": platform.platform(),
    "python": platform.python_version(),
    "studio_home_isolated": str(HOME).startswith(str(Path(os.environ.get("RUNNER_TEMP", HOME.parent)).resolve())),
    "runtime_revision": REVISION,
    "runtime_archive_sha256": ARCHIVE_SHA256,
    "blender_gui_installed": False,
}


def passed(message: str) -> None:
    line = f"PASS: {message}"
    print(line, flush=True)
    passes.append(line)


def request(method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise AssertionError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc


def find_cli() -> Path:
    names = ("unsloth.exe", "unsloth") if os.name == "nt" else ("unsloth",)
    directories = [HOME / "bin", HOME / "studio" / "bin"]
    directories += [p / "Scripts" for p in HOME.glob(".venv*")]
    directories += [p / "bin" for p in HOME.glob(".venv*")]
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    matches = [p for p in HOME.rglob("unsloth*") if p.is_file() and p.name.lower() in names]
    if matches:
        return matches[0]
    raise AssertionError(f"installed Unsloth CLI not found below {HOME}")


def wait_for(path: Path, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file():
            value = path.read_text(encoding="utf-8", errors="ignore").strip()
            if value:
                return value
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {path.name}")


def wait_health(process: subprocess.Popen[Any], timeout: float = 240) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Studio exited before health check (code {process.returncode})")
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise AssertionError("Studio did not become healthy")


def start_studio(log_handle: Any) -> subprocess.Popen[Any]:
    env = dict(os.environ)
    env["UNSLOTH_STUDIO_HOME"] = str(HOME)
    env["UNSLOTH_STUDIO_ALLOW_STDIO_MCP"] = "1"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [str(find_cli()), "studio", "-H", "127.0.0.1", "-p", str(PORT)],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )
    wait_health(process)
    return process


def stop_studio(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        import signal

        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            import signal

            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        process.wait(timeout=10)
    time.sleep(1)


def login(password: str) -> str:
    reply = request("POST", "/api/auth/login", {"username": "unsloth", "password": password})
    token = reply.get("access_token") if isinstance(reply, dict) else None
    assert isinstance(token, str) and token, "login did not return an access token"
    return token


def blender_item(items: Any) -> dict[str, Any]:
    assert isinstance(items, list), "builtins response is not a list"
    matches = [item for item in items if item.get("builtin_id") == "blender"]
    assert len(matches) == 1, f"expected one Blender catalog item, got {len(matches)}"
    return matches[0]


def server_item(items: Any) -> dict[str, Any]:
    assert isinstance(items, list), "servers response is not a list"
    matches = [item for item in items if item.get("builtin_id") == "blender"]
    assert len(matches) == 1, f"expected one persisted Blender server, got {len(matches)}"
    return matches[0]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    assert facts["studio_home_isolated"], "UNSLOTH_STUDIO_HOME is not under RUNNER_TEMP"
    passed("install used an isolated UNSLOTH_STUDIO_HOME")
    cli = find_cli()
    facts["cli_relative_path"] = str(cli.relative_to(HOME)).replace("\\", "/")
    passed("tested-branch Unsloth CLI exists after --local --no-torch install")

    process: subprocess.Popen[Any] | None = None
    rotated = "Live-" + secrets.token_urlsafe(24)
    try:
        with LOG.open("ab", buffering=0) as log_handle:
            process = start_studio(log_handle)
            bootstrap = wait_for(HOME / "auth" / ".bootstrap_password", 30)
            bootstrap_token = login(bootstrap)
            request(
                "POST",
                "/api/auth/change-password",
                {"current_password": bootstrap, "new_password": rotated},
                bootstrap_token,
            )
            token = login(rotated)
            passed("Studio launched healthy and authenticated through the real HTTP API")

            initial_builtin = blender_item(request("GET", "/api/mcp/servers/builtins", token=token))
            initial_servers = request("GET", "/api/mcp/servers/", token=token)
            assert initial_builtin["server_id"] is None and initial_builtin["is_enabled"] is False
            assert not [s for s in initial_servers if s.get("builtin_id") == "blender"]
            assert initial_builtin["available"] is True, initial_builtin.get("unavailable_reason")
            passed("Blender is catalogued but initially unconfigured and disabled")

            enabled = request(
                "PUT",
                "/api/mcp/servers/builtins/blender",
                {"port": 9876, "blender_path": "", "is_enabled": True, "consent": True},
                token,
            )
            assert enabled["server_id"] and enabled["is_enabled"] is True
            runtime = HOME / "cache" / "blender-mcp" / f"{REVISION}-runtime-v1"
            ready = runtime / ".ready"
            assert ready.read_text(encoding="ascii").strip() == ARCHIVE_SHA256
            assert (runtime / "blmcp" / "__init__.py").is_file()
            probe = request(
                "POST",
                "/api/mcp/servers/builtins/blender/test",
                {"port": 9876, "blender_path": "", "consent": False},
                token,
            )
            assert probe["ok"] is True and int(probe["tool_count"]) > 0, probe
            assert probe.get("blender_ready") is False, probe
            assert "not connected" in (probe.get("blender_error") or "").lower(), probe
            facts["tool_count"] = int(probe["tool_count"])
            facts["bridge_limitation"] = probe["blender_error"]
            facts["runtime_ready_relative_path"] = str(ready.relative_to(HOME)).replace("\\", "/")
            passed(f"consent enable checksum-pinned the runtime and discovered {probe['tool_count']} tools via a real MCP subprocess")
            passed("hosted-runner bridge limitation is explicit: Blender GUI/add-on is not installed or connected")

            stop_studio(process)
            process = start_studio(log_handle)
            token = login(rotated)
            persisted_builtin = blender_item(request("GET", "/api/mcp/servers/builtins", token=token))
            persisted_server = server_item(request("GET", "/api/mcp/servers/", token=token))
            assert persisted_builtin["is_enabled"] is True
            assert persisted_server["is_enabled"] is True
            assert persisted_server["id"] == persisted_builtin["server_id"]
            passed("enabled state persisted across restart in /builtins and /api/mcp/servers")

            disabled = request(
                "PUT",
                "/api/mcp/servers/builtins/blender",
                {"port": 9876, "blender_path": "", "is_enabled": False, "consent": False},
                token,
            )
            assert disabled["is_enabled"] is False
            stop_studio(process)
            process = start_studio(log_handle)
            token = login(rotated)
            final_builtin = blender_item(request("GET", "/api/mcp/servers/builtins", token=token))
            final_server = server_item(request("GET", "/api/mcp/servers/", token=token))
            assert final_builtin["is_enabled"] is False and final_server["is_enabled"] is False
            assert ready.read_text(encoding="ascii").strip() == ARCHIVE_SHA256
            passed("disabled state persisted across restart while the checksum-pinned runtime cache remained")
    finally:
        stop_studio(process)
        facts["passes"] = passes
        RESULT.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        SAFE_LOG.write_text("\n".join(passes) + "\n", encoding="utf-8")

    passed("PR #10443 live Blender MCP lifecycle completed")
    facts["passes"] = passes
    RESULT.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    SAFE_LOG.write_text("\n".join(passes) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
