# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Live PR #7660 probe for the unmodified Linux Tauri desktop binary.

The probe drives the production webview through tauri-driver, talks to the real
owned Studio backend, and observes native desktop effects. It adds no product
test hooks: xdg-open is captured through PATH and the native save dialog is
cancelled through X11 input.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


class WebDriver:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id: str | None = None

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 30,
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProbeError(
                f"WebDriver {method} {endpoint}: HTTP {error.code}: {detail}"
            ) from error
        except Exception as error:
            raise ProbeError(f"WebDriver {method} {endpoint}: {error}") from error
        value = body.get("value", body)
        if isinstance(value, dict) and value.get("error"):
            raise ProbeError(
                f"WebDriver {method} {endpoint}: {value.get('error')}: "
                f"{value.get('message')}"
            )
        return value

    def wait_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.request("GET", "/status", timeout=2)
                return
            except Exception as error:  # noqa: BLE001 - retain readiness detail
                last_error = error
                time.sleep(0.5)
        raise ProbeError(f"tauri-driver did not become ready: {last_error}")

    def start_session(self, application: Path) -> None:
        value = self.request(
            "POST",
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "tauri:options": {"application": str(application.resolve())}
                    }
                }
            },
            timeout=120,
        )
        if isinstance(value, dict):
            self.session_id = value.get("sessionId")
        if not self.session_id:
            raise ProbeError(f"new WebDriver session had no sessionId: {value!r}")

    def endpoint(self, suffix: str) -> str:
        if not self.session_id:
            raise ProbeError("WebDriver session has not started")
        return f"/session/{self.session_id}{suffix}"

    def source(self) -> str:
        return str(self.request("GET", self.endpoint("/source"), timeout=30))

    def execute(self, script: str, *args: Any) -> Any:
        return self.request(
            "POST",
            self.endpoint("/execute/sync"),
            {"script": script, "args": list(args)},
            timeout=30,
        )

    def click_aria(self, label: str) -> None:
        clicked = self.execute(
            """
            const wanted = arguments[0];
            const el = [...document.querySelectorAll('button')].find(
              (node) => node.getAttribute('aria-label') === wanted &&
                node.getClientRects().length > 0
            );
            if (!el) return false;
            el.click();
            return true;
            """,
            label,
        )
        if clicked is not True:
            raise ProbeError(f"visible button with aria-label {label!r} was not found")

    def click_text(self, text: str, *, starts_with: bool = False) -> None:
        clicked = self.execute(
            """
            const wanted = arguments[0];
            const starts = arguments[1];
            const el = [...document.querySelectorAll('button')].find((node) => {
              if (node.getClientRects().length === 0) return false;
              const actual = (node.textContent || '').trim().replace(/\\s+/g, ' ');
              return starts ? actual.startsWith(wanted) : actual === wanted;
            });
            if (!el) return false;
            el.click();
            return true;
            """,
            text,
            starts_with,
        )
        if clicked is not True:
            raise ProbeError(f"visible button with text {text!r} was not found")

    def screenshot(self, path: Path) -> None:
        encoded = self.request("GET", self.endpoint("/screenshot"), timeout=30)
        path.write_bytes(base64.b64decode(encoded))

    def close(self) -> None:
        if not self.session_id:
            return
        try:
            self.request("DELETE", self.endpoint(""), timeout=30)
        except Exception:
            pass
        self.session_id = None


def wait_for(predicate, timeout: float, description: str, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as error:  # noqa: BLE001 - report final poll error
            last_error = error
        time.sleep(interval)
    suffix = f"; last error: {last_error}" if last_error else ""
    raise ProbeError(f"timed out waiting for {description}{suffix}")


def desktop_screenshot(path: Path) -> None:
    completed = subprocess.run(
        ["scrot", "--silent", "--overwrite", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ProbeError(f"scrot failed: {completed.stdout.strip()}")


def health_port() -> tuple[int, dict[str, Any]] | None:
    for port in range(8888, 8909):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "healthy":
                return port, payload
        except Exception:
            continue
    return None


def api_json(
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ProbeError(
            f"API {method} {path}: HTTP {error.code}: {detail}"
        ) from error


def seed_chat_attachment(base_url: str, token: str) -> None:
    now = int(time.time() * 1000)
    thread_id = "pr7660-live-thread"
    message_id = "pr7660-live-message"
    api_json(
        base_url,
        token,
        "POST",
        "/api/chat/threads",
        {
            "id": thread_id,
            "title": "PR 7660 live attachment",
            "modelType": "base",
            "modelId": "",
            "createdAt": now,
            "updatedAt": now,
        },
    )
    api_json(
        base_url,
        token,
        "PUT",
        f"/api/chat/threads/{thread_id}/messages/{message_id}",
        {
            "id": message_id,
            "threadId": thread_id,
            "role": "user",
            "content": [{"type": "text", "text": "Live attachment seed"}],
            "attachments": [
                {
                    "id": "pr7660-attachment",
                    "type": "document",
                    "name": "pr7660-report.pdf",
                    "content": [
                        {
                            "type": "text",
                            "text": "PR 7660 bearer-gated attachment bytes",
                        }
                    ],
                }
            ],
            "createdAt": now,
        },
    )
    listing = api_json(base_url, token, "GET", "/api/chat/attachments?limit=50&offset=0")
    names = [item.get("name") for item in listing.get("attachments", [])]
    if "pr7660-report.pdf" not in names:
        raise ProbeError(f"seeded chat attachment missing from live API: {names!r}")


def seed_rag_document(repo: Path, studio_home: Path, evidence: Path) -> None:
    venv_python = studio_home / "unsloth_studio" / "bin" / "python"
    if not venv_python.is_file():
        raise ProbeError(f"managed Studio Python is missing: {venv_python}")
    seed_script = evidence / "seed-rag.py"
    seed_script.write_text(
        """from __future__ import annotations
import hashlib
import json
from core.rag import store
from storage import rag_db
from utils.paths import ensure_dir, rag_uploads_root

payload = b'PR 7660 signed RAG file bytes\\n'
path = ensure_dir(rag_uploads_root()) / 'pr7660-rag.txt'
path.write_bytes(payload)
conn = rag_db.get_connection()
try:
    old = store.get_document(conn, 'pr7660-rag-document')
    if old is None:
        store.create_document(
            conn,
            scope='pr7660-live',
            filename='pr7660-rag.txt',
            sha256=hashlib.sha256(payload).hexdigest(),
            status='completed',
            stored_path=str(path),
            document_id='pr7660-rag-document',
        )
finally:
    conn.close()
print(json.dumps({'documentId': 'pr7660-rag-document', 'path': str(path)}))
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo / "studio" / "backend")
    completed = subprocess.run(
        [str(venv_python), str(seed_script)],
        cwd=repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    (evidence / "seed-rag.log").write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise ProbeError(f"RAG seed failed ({completed.returncode}): {completed.stdout}")


def visible_windows() -> set[str]:
    completed = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--name", ".*"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def active_window() -> str | None:
    completed = subprocess.run(
        ["xdotool", "getactivewindow"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def window_name(window_id: str) -> str:
    completed = subprocess.run(
        ["xdotool", "getwindowname", window_id],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=10,
    )
    return completed.stdout.strip()


def collect_logs(studio_home: Path, evidence: Path) -> None:
    logs = evidence / "logs"
    logs.mkdir(exist_ok=True)
    candidates = [
        studio_home / "tauri.log",
        studio_home / "logs",
        Path.home() / ".local" / "share" / "unsloth" / "tauri.log",
    ]
    for candidate in candidates:
        if candidate.is_file():
            shutil.copy2(candidate, logs / candidate.name)
        elif candidate.is_dir():
            for source in candidate.rglob("*"):
                if source.is_file() and source.stat().st_size < 20_000_000:
                    destination = logs / source.name
                    try:
                        shutil.copy2(source, destination)
                    except OSError:
                        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--driver-port", type=int, default=4444)
    args = parser.parse_args()

    repo = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())).resolve()
    application = args.application.resolve()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    screenshots = evidence / "screenshots"
    screenshots.mkdir(exist_ok=True)
    # The desktop binary deliberately owns only the legacy root and removes
    # UNSLOTH_STUDIO_HOME from every managed subprocess.
    studio_home = (Path.home() / ".unsloth" / "studio").resolve()

    opener_bin = evidence / "fake-bin"
    opener_bin.mkdir(exist_ok=True)
    opener_log = evidence / "xdg-open-url.txt"
    opener = opener_bin / "xdg-open"
    opener.write_text(
        "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$@\" > \"${PR7660_OPEN_LOG:?}\"\n",
        encoding="utf-8",
    )
    opener.chmod(0o755)

    driver_log = evidence / "tauri-driver.log"
    environment = os.environ.copy()
    environment["PATH"] = f"{opener_bin}{os.pathsep}{environment['PATH']}"
    environment["PR7660_OPEN_LOG"] = str(opener_log)
    with driver_log.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            ["tauri-driver", "--port", str(args.driver_port)],
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    driver = WebDriver(f"http://127.0.0.1:{args.driver_port}")
    result: dict[str, Any] = {
        "commit": os.environ.get("GITHUB_SHA"),
        "application": str(application),
        "checks": [],
        "status": "failed",
    }
    try:
        driver.wait_ready()
        driver.start_session(application)
        health = wait_for(health_port, 300, "owned Studio backend health", interval=2)
        port, health_payload = health
        base_url = f"http://127.0.0.1:{port}"
        (evidence / "backend-health.json").write_text(
            json.dumps(health_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        wait_for(
            lambda: "New chat" in driver.source(),
            300,
            "usable desktop chat UI",
            interval=2,
        )
        initial_source = driver.source()
        (evidence / "01-initial-source.html").write_text(
            initial_source, encoding="utf-8", errors="replace"
        )
        desktop_screenshot(screenshots / "01-live-desktop.png")

        token = wait_for(
            lambda: driver.execute(
                "return window.localStorage.getItem('unsloth_auth_token');"
            ),
            90,
            "desktop auth token in the live webview",
        )
        if not isinstance(token, str) or len(token) < 16:
            raise ProbeError("desktop auth token was not a usable string")

        seed_chat_attachment(base_url, token)
        seed_rag_document(repo, studio_home, evidence)
        result["checks"].append("live backend seeded RAG and bearer-gated chat records")
        print("PASS live backend seeded RAG and bearer-gated chat records", flush=True)

        driver.click_aria("Settings")
        wait_for(lambda: "Settings" in driver.source(), 30, "Settings dialog")
        driver.click_text("Data", starts_with=True)
        wait_for(lambda: "Uploaded files" in driver.source(), 30, "Data settings tab")
        opened_files = driver.execute(
            """
            const row = document.querySelector('[data-settings-label="Uploaded files"]');
            const button = row && [...row.querySelectorAll('button')].find(
              (node) => (node.textContent || '').trim() === 'Manage'
            );
            if (!button || button.getClientRects().length === 0) return false;
            button.click();
            return true;
            """
        )
        if (opened_files is not True:
            raise ProbeError("Uploaded files Manage action was not found")
        wait_for(
            lambda: (
                "pr7660-rag.txt" in driver.source()
                and "pr7660-report.pdf" in driver.source()
            ),
            60,
            "both uploaded-file rows",
        )
        files_source = driver.source()
        (evidence / "02-uploaded-files-source.html").write_text(
            files_source, encoding="utf-8", errors="replace"
        )
        desktop_screenshot(screenshots / "02-uploaded-files.png")
        result["checks"].append("Settings > Data rendered both live file records")
        print("PASS Settings > Data rendered both live file records", flush=True)

        driver.click_aria("Open pr7660-rag.txt")
        opened_url = wait_for(
            lambda: opener_log.read_text(encoding="utf-8").strip()
            if opener_log.is_file()
            else None,
            45,
            "OS xdg-open invocation for the RAG file",
        )
        expected_prefix = f"{base_url}/api/rag/documents/pr7660-rag-document/file-signed?token="
        if not opened_url.startswith(expected_prefix):
            raise ProbeError(
                f"OS opener did not receive the absolute signed backend URL: {opened_url!r}"
            )
        with urllib.request.urlopen(opened_url, timeout=30) as response:
            signed_bytes = response.read()
        if signed_bytes != b"PR 7660 signed RAG file bytes\n":
            raise ProbeError(f"signed no-bearer URL returned wrong bytes: {signed_bytes!r}")
        desktop_screenshot(screenshots / "03-rag-opened-via-os.png")
        result["opened_url"] = opened_url
        result["checks"].append(
            "RAG Open invoked the OS with an absolute signed URL fetchable without bearer auth"
        )
        print(
            "PASS RAG Open invoked OS with absolute signed URL and exact bytes",
            flush=True,
        )

        baseline_windows = visible_windows()
        driver.click_aria("Open pr7660-report.pdf")

        def new_dialog() -> str | None:
            active = active_window()
            current = visible_windows()
            if active and active not in baseline_windows:
                return active
            added = sorted(current - baseline_windows)
            return added[-1] if added else None

        dialog_window = wait_for(new_dialog, 45, "native save-file chooser")
        dialog_title = window_name(dialog_window)
        (evidence / "native-save-dialog.json").write_text(
            json.dumps(
                {"windowId": dialog_window, "title": dialog_title},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        desktop_screenshot(screenshots / "04-chat-native-save-dialog.png")
        subprocess.run(
            ["xdotool", "key", "--window", dialog_window, "Escape"],
            check=True,
            timeout=15,
        )
        wait_for(
            lambda: dialog_window not in visible_windows(),
            30,
            "native save dialog cancellation",
        )
        time.sleep(2)
        after_cancel_source = driver.source()
        (evidence / "03-after-cancel-source.html").write_text(
            after_cancel_source, encoding="utf-8", errors="replace"
        )
        if "Failed to open file" in after_cancel_source:
            raise ProbeError("native save cancellation incorrectly surfaced a failure toast")
        desktop_screenshot(screenshots / "05-chat-save-cancelled-silently.png")
        result["native_dialog_title"] = dialog_title
        result["checks"].append(
            "bearer-gated chat attachment opened a native save chooser and cancellation was silent"
        )
        print(
            "PASS chat attachment opened native save chooser; cancellation stayed silent",
            flush=True,
        )

        result["status"] = "passed"
        (evidence / "PASS.txt").write_text(
            "PASS PR 7660 live Tauri uploaded-file flow\n", encoding="utf-8"
        )
        return 0
    except Exception as error:  # noqa: BLE001 - preserve all live evidence
        result["error"] = str(error)
        print(f"FAIL PR 7660 live Tauri probe: {error}", flush=True)
        try:
            (evidence / "failure-source.html").write_text(
                driver.source(), encoding="utf-8", errors="replace"
            )
        except Exception:
            pass
        try:
            desktop_screenshot(screenshots / "99-failure.png")
        except Exception:
            pass
        return 1
    finally:
        result["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (evidence / "results.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        collect_logs(studio_home, evidence)
        driver.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
