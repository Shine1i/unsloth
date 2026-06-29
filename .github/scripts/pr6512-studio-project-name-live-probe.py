#!/usr/bin/env python3
"""Live Studio probe for PR 6512 project-name run history behavior.

Do not print bootstrap passwords, bearer tokens, or provider keys.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import httpx
from studio_test_kit.auth import login, seed_init_script
from studio_test_kit.ui import open_chat

PROJECT_NAME = "Customer Support / LoRA v2"
NORMALIZED_PROJECT = "Customer Support / LoRA v2"
PROJECT_SLUG = "customer-support-lora-v2"
MODEL_NAME = "unsloth/Llama-3.2-3B-Instruct"
RUN_ID = "ci-project-name-run"
RUN_TIMESTAMP = 1771227800
RUN_DIR_NAME = f"unsloth_Llama-3.2-3B-Instruct__project-{PROJECT_SLUG}_{RUN_TIMESTAMP}"


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
    paths = ("/healthz", "/api/health", "/")
    while time.time() < deadline:
        for path in paths:
            try:
                with urllib.request.urlopen(f"{base_url}{path}", timeout=3) as resp:
                    if resp.status < 500:
                        return path
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
        time.sleep(2)
    fail(f"Studio did not become healthy within {timeout_s}s")


def studio_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(home)
    env.pop("STUDIO_HOME", None)
    return env


def start_studio(home: Path, log_path: Path, port: int) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    cmd = [str(find_unsloth_bin(home)), "studio", "-H", "127.0.0.1", "-p", str(port)]
    print("Launching Studio for project-name probe", flush=True)
    kwargs: dict = {"stdout": log_handle, "stderr": subprocess.STDOUT, "env": studio_env(home)}
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
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()


def seed_project_run(home: Path) -> Path:
    outputs = home / "outputs"
    run_dir = outputs / RUN_DIR_NAME
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "adapter_config.json").write_text(
        json.dumps({"base_model_name_or_path": MODEL_NAME, "peft_type": "LORA", "r": 16}),
        encoding="utf-8",
    )
    checkpoint = run_dir / "checkpoint-12"
    checkpoint.mkdir(exist_ok=True)
    (checkpoint / "trainer_state.json").write_text(
        json.dumps({"log_history": [{"step": 12, "loss": 1.234}]}),
        encoding="utf-8",
    )

    db = home / "studio.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        config = {
            "model_name": MODEL_NAME,
            "project_name": f"  {PROJECT_NAME}  ",
            "training_type": "LoRA/QLoRA",
            "load_in_4bit": True,
            "hf_dataset": "ci/project-name-dataset",
            "num_epochs": 1,
            "batch_size": 1,
            "learning_rate": "2e-4",
            "max_steps": 12,
            "max_seq_length": 2048,
            "warmup_steps": 0,
            "optim": "adamw_8bit",
            "lora_r": 16,
            "lora_alpha": 16,
            "lora_dropout": 0.0,
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO training_runs (
              id, status, model_name, dataset_name, config_json, started_at,
              ended_at, total_steps, final_step, final_loss, output_dir,
              duration_seconds, loss_sparkline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RUN_ID,
                "completed",
                MODEL_NAME,
                "ci/project-name-dataset",
                json.dumps(config),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                12,
                12,
                1.234,
                str(run_dir.resolve()),
                42.0,
                json.dumps([2.0, 1.5, 1.234]),
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO training_metrics
              (run_id, step, loss, learning_rate, grad_norm, epoch, elapsed_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (RUN_ID, 12, 1.234, 0.0002, 0.5, 1.0, 42.0),
        )
        conn.commit()
    finally:
        conn.close()
    return run_dir


async def auth_init(base_url: str, password: str) -> str:
    auth = await login(base_url, "unsloth", password)
    if auth.must_change_password:
        new_password = os.environ.get("STUDIO_TEST_PASSWORD", "UnslothStudioCI2026!")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{base_url}/api/auth/change-password",
                headers={"Authorization": f"Bearer {auth.access_token}"},
                json={"current_password": password, "new_password": new_password},
            )
            response.raise_for_status()
            body = response.json()
        auth.access_token = body["access_token"]
        auth.refresh_token = body.get("refresh_token", "")
    pass_log("Studio API login succeeded")
    return seed_init_script(auth, [])


async def warm_training_schema(base_url: str, init_script: str) -> None:
    token_match = re.search(r'"unsloth_auth_token"\s*:\s*"([^"]+)"', init_script)
    if not token_match:
        fail("could not extract seeded auth token for schema warmup")
    headers = {"Authorization": f"Bearer {token_match.group(1)}"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        response = await client.get(f"{base_url}/api/train/runs?limit=1&offset=0")
        response.raise_for_status()
    pass_log("Training history schema initialized")

async def assert_api(base_url: str, init_script: str) -> None:
    # Extract the bearer token from the init script without printing it.
    token_match = re.search(r'"unsloth_auth_token"\s*:\s*"([^"]+)"', init_script)
    if not token_match:
        fail("could not extract seeded auth token for API assertions")
    headers = {"Authorization": f"Bearer {token_match.group(1)}"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        runs_response = await client.get(f"{base_url}/api/train/runs?limit=5&offset=0")
        runs_response.raise_for_status()
        runs = runs_response.json()
        run = next((item for item in runs.get("runs", []) if item.get("id") == RUN_ID), None)
        if not run:
            fail("seeded training run was not returned by /api/train/runs")
        if run.get("project_name") != NORMALIZED_PROJECT:
            fail(f"list run project_name mismatch: {run.get('project_name')!r}")
        detail_response = await client.get(f"{base_url}/api/train/runs/{RUN_ID}")
        detail_response.raise_for_status()
        detail = detail_response.json()
        if detail.get("run", {}).get("project_name") != NORMALIZED_PROJECT:
            fail("detail run project_name was not normalized/preserved")
        if detail.get("config", {}).get("project_name", "").strip() != PROJECT_NAME:
            fail("detail config did not preserve project_name for resume/start payloads")
        checkpoints_response = await client.get(f"{base_url}/api/models/checkpoints")
        checkpoints_response.raise_for_status()
        checkpoints = checkpoints_response.json()
        models = checkpoints.get("models") if isinstance(checkpoints, dict) else checkpoints
        matched = False
        for model in models or []:
            checkpoint_names = [cp.get("display_name") for cp in model.get("checkpoints", [])]
            if RUN_DIR_NAME in checkpoint_names:
                matched = True
                if model.get("base_model") != MODEL_NAME:
                    fail(f"checkpoint base_model mismatch: {model.get('base_model')!r}")
        if not matched:
            fail("project-suffixed checkpoint folder was not listed by /api/models/checkpoints")
    pass_log("API exposes normalized project_name and project-suffixed checkpoint metadata")


async def assert_ui(base_url: str, init_script: str, browser_name: str, artifact_dir: Path) -> None:
    async with open_chat(
        base_url,
        init_scripts=[init_script],
        video_dir=None,
        browser_name=browser_name,
    ) as sp:
        page = sp.page
        await page.goto(f"{base_url}/studio", wait_until="domcontentloaded")
        await page.get_by_role("tab", name=re.compile("History", re.I)).click(timeout=30_000)
        project = page.get_by_text(NORMALIZED_PROJECT, exact=True).first
        await project.wait_for(state="visible", timeout=30_000)
        await sp.screenshot(artifact_dir / f"history-project-name-{browser_name}.png")
        await project.click(timeout=30_000)
        await page.get_by_text(NORMALIZED_PROJECT, exact=True).first.wait_for(
            state="visible",
            timeout=30_000,
        )
        await sp.screenshot(artifact_dir / f"historical-project-name-{browser_name}.png")
    pass_log(f"Playwright {browser_name} shows project name in history and historical run views")


async def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    browser_name = os.environ.get("STUDIO_BROWSER", "chromium")
    artifact_dir = Path(os.environ.get("STUDIO_ARTIFACT_DIR", "studio-live-artifacts")).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = artifact_dir / "studio.log"
    proc: subprocess.Popen | None = None
    try:
        proc = start_studio(home, log_path, port)
        health_path = wait_for_health(base_url)
        pass_log(f"Studio healthy at {health_path}")
        password = read_bootstrap_password(home, log_path)
        if not password:
            fail("could not read Studio bootstrap password")
        init_script = await auth_init(base_url, password)
        await warm_training_schema(base_url, init_script)
        run_dir = seed_project_run(home)
        pass_log(f"Seeded project-name run under outputs/{run_dir.name}")
        await assert_api(base_url, init_script)
        await assert_ui(base_url, init_script, browser_name, artifact_dir)
    finally:
        stop_process(proc)


if __name__ == "__main__":
    asyncio.run(main())
