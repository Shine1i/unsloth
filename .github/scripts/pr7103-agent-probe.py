#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Run all six coding-agent CLIs through `unsloth start` against live Studio."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

AGENTS = ("claude", "codex", "hermes", "openclaw", "opencode", "pi")
PROMPT = "Reply with exactly the single word: pong"
ERROR_MARKERS = (
    "connection refused",
    "econnrefused",
    "unauthorized",
    "invalid api key",
    "authentication failed",
    "context overflow",
    "message too long",
    "response.failed",
)


def redact(text: str) -> str:
    token = os.environ.get("UNSLOTH_API_KEY", "")
    return text.replace(token, "<REDACTED>") if token else text


def run(
    command: list[str], *, timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    print("RUN:", shlex.join(command[:4] + (["..."] if len(command) > 4 else [])))
    return subprocess.run(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        errors="replace",
    )


def env_value(output: str, name: str) -> str:
    posix = re.findall(rf"^export {re.escape(name)}=(.+)$", output, re.MULTILINE)
    if posix:
        try:
            return shlex.split(posix[-1])[0]
        except (ValueError, IndexError):
            return posix[-1].strip("'\"")
    powershell = re.findall(
        rf'^\$env:{re.escape(name)}\s*=\s*"((?:[^"`]|`.)*)"$', output, re.MULTILINE
    )
    if powershell:
        return powershell[-1].replace('`"', '"').replace("``", "`")
    raise RuntimeError(f"{name} was absent from --no-launch output")


def prepare_relocated_config(agent: str, base: list[str], logs: Path) -> None:
    if agent not in ("hermes", "openclaw"):
        return
    probe = run(base + ["--persist", "--no-launch"], timeout=120)
    (logs / f"{agent}-no-launch.txt").write_text(redact(probe.stdout), encoding="utf-8")
    if probe.returncode:
        raise RuntimeError(f"--no-launch exited {probe.returncode}")

    if agent == "hermes":
        config_path = Path(env_value(probe.stdout, "HERMES_HOME")) / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        toolsets = config.setdefault("platform_toolsets", {})
        toolsets["cli"] = []
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    else:
        config_path = Path(env_value(probe.stdout, "OPENCLAW_CONFIG_PATH"))
        config = json.loads(config_path.read_text(encoding="utf-8"))
        agents = config.setdefault("agents", {})
        agents.setdefault("defaults", {})["skipBootstrap"] = True
        entries = [entry for entry in agents.get("list", []) if entry.get("id") != "ci"]
        entries.append(
            {
                "id": "ci",
                "contextInjection": "never",
                "tools": {"deny": ["*"]},
            }
        )
        agents["list"] = entries
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def command_for(agent: str, base: list[str], root: Path) -> list[str]:
    if agent == "claude":
        return base + [
            "--system-prompt-file",
            str(root / ".github/scripts/ci-connect-prompt.txt"),
            "--tools",
            "",
            "-p",
            PROMPT,
        ]
    if agent == "codex":
        return base + ["exec", PROMPT]
    if agent == "hermes":
        return base + ["--persist", "-z", PROMPT]
    if agent == "openclaw":
        model = os.environ["UNSLOTH_MODEL_ID"]
        return base + [
            "--persist",
            "--",
            "agent",
            "--local",
            "--agent",
            "ci",
            "--model",
            f"unsloth/{model}",
            "--message",
            PROMPT,
        ]
    if agent == "opencode":
        return base + ["run", PROMPT]
    return base + ["-p", PROMPT]


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    logs = root / "logs" / "pr7103-agent-probes"
    logs.mkdir(parents=True, exist_ok=True)
    token = os.environ["UNSLOTH_API_KEY"]
    base_url = os.environ["UNSLOTH_STUDIO_URL"]
    os.environ["UNSLOTH_STUDIO_URL"] = base_url
    os.environ["UNSLOTH_BASE_URL"] = base_url
    os.environ["IS_SANDBOX"] = "1"
    timeout = int(os.environ.get("AGENT_INVOKE_TIMEOUT", "900"))
    failures: list[str] = []

    for agent in AGENTS:
        print(f"\n===== {agent} =====")
        executable = shutil.which(agent)
        if not executable:
            failures.append(f"{agent}: executable not found")
            print(f"FAIL {failures[-1]}")
            continue
        try:
            version = run([agent, "--version"], timeout=60)
            print(redact(version.stdout).strip()[:500])
            base = ["unsloth", "start", agent, "--yolo", "--api-key", token]
            prepare_relocated_config(agent, base, logs)
            work = root / "agent-workdir" / agent
            work.mkdir(parents=True, exist_ok=True)
            command = command_for(agent, base, root)
            result = run(command, timeout=timeout, cwd=work)
            output = redact(result.stdout)
            if result.returncode:
                print(f"Retrying {agent} once after exit {result.returncode}")
                result = run(command, timeout=timeout, cwd=work)
                output += "\n===== RETRY =====\n" + redact(result.stdout)
            (logs / f"{agent}.txt").write_text(output, encoding="utf-8")
            print(output[-4000:])
            lowered = output.lower()
            if result.returncode:
                raise RuntimeError(f"launch exited {result.returncode}")
            if not output.strip():
                raise RuntimeError("launch produced no output")
            marker = next((marker for marker in ERROR_MARKERS if marker in lowered), None)
            if marker:
                raise RuntimeError(f"launch output contained {marker!r}")
            if agent == "codex" and (
                "fallback metadata" in lowered or "model metadata for" in lowered
            ):
                raise RuntimeError("Codex emitted the fallback model-metadata warning")
            print(f"PASS agent={agent} platform={sys.platform}")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            failure = f"{agent}: {type(error).__name__}: {error}"
            failures.append(failure)
            print(f"FAIL {failure}")

    if failures:
        print("\nAgent failures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"\nPASS all six agents platform={sys.platform}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
