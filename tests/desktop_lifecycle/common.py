# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Evidence helpers for destructive desktop lifecycle tests.

This module intentionally has no third-party dependencies.  The lifecycle
workflow runs on fresh GitHub-hosted runners and in disposable Distrobox homes,
so evidence collection must work before the application or Playwright is
installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ALLOWED_STATUSES = {"verified", "failed", "not reproducible", "blocked"}
SAFE_ENV_KEYS = (
    "CI",
    "GITHUB_ACTION",
    "GITHUB_ACTOR",
    "GITHUB_JOB",
    "GITHUB_REF",
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_NUMBER",
    "GITHUB_SHA",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "RUNNER_ARCH",
    "RUNNER_NAME",
    "RUNNER_OS",
    "RUNNER_TEMP",
    "RUNNER_TOOL_CACHE",
    "SHELL",
    "USER",
    "USERNAME",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def quote_command(command: Sequence[str] | str) -> str:
    if isinstance(command, str):
        return command
    if os.name == "nt":
        return subprocess.list2cmdline([str(part) for part in command])
    return shlex.join([str(part) for part in command])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}


def resolve_installed_cli() -> Path:
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "unsloth",
        home / ".local" / "bin" / "unsloth.exe",
        home / ".unsloth" / "studio" / "bin" / "unsloth.exe",
        home / ".unsloth" / "studio" / "unsloth_studio" / "bin" / "unsloth",
        home / ".unsloth" / "studio" / "unsloth_studio" / "Scripts" / "unsloth.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    found = shutil.which("unsloth")
    if found:
        return Path(found).resolve()
    raise FileNotFoundError(
        "Installed Unsloth CLI not found; checked " + ", ".join(map(str, candidates))
    )


def run_installed_web_ui_smoke(
    evidence: "EvidenceRun",
    playwright_script: str | Path,
    *,
    port: int = 18892,
    timeout: float = 300,
) -> list[Path]:
    """Launch the installed browser UI and exercise it through Playwright.

    The packaged desktop intentionally owns an API-only backend, so its embedded
    UI is driven by Tauri WebDriver/native input. This separate process verifies
    the same freshly installed CLI's real HTTP-served UI without treating the
    API-only backend's expected root 404 as a product failure.
    """

    cli = resolve_installed_cli()
    command = [
        str(cli),
        "studio",
        "-H",
        "127.0.0.1",
        "-p",
        str(port),
        "--disable-tools",
    ]
    command_path = evidence.output_dir / "browser-ui-command.txt"
    command_path.write_text(quote_command(command) + "\n", encoding="utf-8")
    server_log = evidence.logs_dir / "installed-browser-ui.log"
    environment = os.environ.copy()
    environment["UNSLOTH_SKIP_UPDATE_CHECK"] = "1"
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    with server_log.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        try:
            deadline = time.monotonic() + timeout
            health: dict[str, Any] | None = None
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Installed browser UI exited {process.returncode}; see {server_log}"
                    )
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=3
                    ) as response:
                        candidate = json.loads(
                            response.read().decode("utf-8", errors="replace")
                        )
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/", timeout=3
                    ) as response:
                        root_status = response.status
                    if candidate.get("status") == "healthy" and root_status < 400:
                        health = candidate
                        break
                except Exception as error:
                    last_error = error
                time.sleep(1)
            if health is None:
                raise RuntimeError(f"Installed browser UI health timeout: {last_error}")
            health_path = evidence.output_dir / "browser-ui-health.json"
            health_path.write_text(
                json.dumps(health, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            artifact_dir = evidence.screenshots_dir / "playwright"
            completed = evidence.run(
                [sys.executable, str(playwright_script)],
                name="playwright-installed-web-ui",
                env={
                    "BASE_URL": f"http://127.0.0.1:{port}",
                    "PW_ART_DIR": str(artifact_dir),
                },
                timeout=600,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Installed browser UI Playwright smoke failed with "
                    f"{completed.returncode}"
                )
            return [
                command_path,
                server_log,
                health_path,
                artifact_dir / "health.json",
                artifact_dir / "page.json",
                artifact_dir / "web-ui.png",
                evidence.logs_dir / "playwright-installed-web-ui.log",
            ]
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()


def machine_facts() -> dict[str, Any]:
    facts: dict[str, Any] = {
        "captured_at": utc_now(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version,
        "environment": safe_environment(),
    }
    if Path("/etc/os-release").is_file():
        facts["os_release"] = Path("/etc/os-release").read_text(
            encoding="utf-8", errors="replace"
        )
    return facts


@dataclass
class CommandEvidence:
    command: str
    cwd: str
    started_at: str
    duration_seconds: float
    exit_code: int
    log: str


@dataclass
class ScenarioResult:
    scenario: str
    status: str
    summary: str
    started_at: str
    completed_at: str
    platform: str
    commands: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    mismatch: str | None = None
    limitation: str | None = None


class EvidenceRun:
    """Collect command output and scenario dispositions in one artifact tree."""

    def __init__(self, output_dir: str | Path, suite: str) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.output_dir / "logs"
        self.screenshots_dir = self.output_dir / "screenshots"
        self.snapshots_dir = self.output_dir / "snapshots"
        self.logs_dir.mkdir(exist_ok=True)
        self.screenshots_dir.mkdir(exist_ok=True)
        self.snapshots_dir.mkdir(exist_ok=True)
        self.suite = suite
        self.started_at = utc_now()
        self.commands: list[CommandEvidence] = []
        self.results: list[ScenarioResult] = []
        self._scenario_start: dict[str, str] = {}
        self._write_json("environment.json", machine_facts())

    def _write_json(self, name: str, value: Any) -> Path:
        path = self.output_dir / name
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def begin(self, scenario: str) -> None:
        self._scenario_start[scenario] = utc_now()

    def run(
        self,
        command: Sequence[str] | str,
        *,
        name: str,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        check: bool = True,
        shell: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        log_path = self.logs_dir / f"{name}.log"
        command_text = quote_command(command)
        started_at = utc_now()
        started = time.monotonic()
        merged_env = os.environ.copy()
        if env:
            merged_env.update({str(key): str(value) for key, value in env.items()})
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=merged_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                shell=shell,
                check=False,
            )
            output = completed.stdout
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as error:
            output = (error.stdout or "") + (error.stderr or "")
            exit_code = 124
            completed = subprocess.CompletedProcess(command, exit_code, output, None)
        duration = time.monotonic() - started
        log_path.write_text(
            "\n".join(
                (
                    f"started_at={started_at}",
                    f"cwd={Path(cwd or os.getcwd()).resolve()}",
                    f"command={command_text}",
                    f"duration_seconds={duration:.3f}",
                    f"exit_code={exit_code}",
                    "",
                    output,
                )
            ),
            encoding="utf-8",
            errors="replace",
        )
        self.commands.append(
            CommandEvidence(
                command=command_text,
                cwd=str(Path(cwd or os.getcwd()).resolve()),
                started_at=started_at,
                duration_seconds=round(duration, 3),
                exit_code=exit_code,
                log=str(log_path.relative_to(self.output_dir)),
            )
        )
        self.flush()
        if check and exit_code != 0:
            raise subprocess.CalledProcessError(exit_code, command, output=output)
        return completed

    def snapshot_processes(self, label: str) -> Path:
        path = self.snapshots_dir / f"{label}-processes.txt"
        if os.name == "nt":
            command = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | "
                "Format-List",
            ]
        else:
            command = ["ps", "-eo", "pid,ppid,pgid,sid,lstart,stat,args"]
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        path.write_text(completed.stdout, encoding="utf-8", errors="replace")
        return path

    def snapshot_tree(
        self,
        label: str,
        roots: Iterable[str | Path],
        *,
        hash_files_under: int = 4 * 1024 * 1024,
    ) -> Path:
        """Record metadata and small-file hashes without copying user contents."""

        records: list[dict[str, Any]] = []
        for raw_root in roots:
            root = Path(raw_root).expanduser()
            root_record: dict[str, Any] = {
                "root": str(root),
                "exists": root.exists(),
            }
            records.append(root_record)
            if not root.exists():
                continue
            paths = [root]
            if root.is_dir():
                paths.extend(sorted(root.rglob("*"), key=lambda value: str(value)))
            for path in paths:
                try:
                    stat = path.lstat()
                except OSError as error:
                    records.append({"path": str(path), "error": str(error)})
                    continue
                record: dict[str, Any] = {
                    "path": str(path),
                    "kind": (
                        "symlink"
                        if path.is_symlink()
                        else "dir"
                        if path.is_dir()
                        else "file"
                        if path.is_file()
                        else "other"
                    ),
                    "mode": oct(stat.st_mode & 0o7777),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
                if path.is_symlink():
                    try:
                        record["target"] = os.readlink(path)
                    except OSError as error:
                        record["target_error"] = str(error)
                elif path.is_file() and stat.st_size <= hash_files_under:
                    try:
                        record["sha256"] = sha256_file(path)
                    except OSError as error:
                        record["sha256_error"] = str(error)
                records.append(record)
        path = self.snapshots_dir / f"{label}-filesystem.json"
        path.write_text(
            json.dumps(records, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def copy_artifact(self, source: str | Path, name: str | None = None) -> Path:
        source_path = Path(source)
        destination = self.output_dir / (name or source_path.name)
        shutil.copy2(source_path, destination)
        return destination

    def record(
        self,
        scenario: str,
        status: str,
        summary: str,
        *,
        evidence: Iterable[str | Path] = (),
        commands: Iterable[str] = (),
        mismatch: str | None = None,
        limitation: str | None = None,
    ) -> ScenarioResult:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Unsupported scenario status: {status}")
        result = ScenarioResult(
            scenario=scenario,
            status=status,
            summary=summary,
            started_at=self._scenario_start.pop(scenario, self.started_at),
            completed_at=utc_now(),
            platform=platform.platform(),
            commands=list(commands),
            evidence=[str(Path(item)) for item in evidence],
            mismatch=mismatch,
            limitation=limitation,
        )
        self.results = [item for item in self.results if item.scenario != scenario]
        self.results.append(result)
        self.flush()
        return result

    def flush(self) -> None:
        payload = {
            "schema": 1,
            "suite": self.suite,
            "started_at": self.started_at,
            "updated_at": utc_now(),
            "results": [
                asdict(result) for result in sorted(self.results, key=lambda item: item.scenario)
            ],
            "commands": [asdict(command) for command in self.commands],
        }
        self._write_json("results.json", payload)
