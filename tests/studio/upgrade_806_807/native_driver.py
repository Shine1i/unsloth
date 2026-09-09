# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0
"""Drive official 806 -> 807 binaries, not a source build or backend fixture.

This is explicitly IPC-driven, following 806 use-tauri-update.ts's staged fast
path: check, stage backend, download signed shell, stop_server, install,
mark_in_app_relaunch, plugin:process|restart. It does NOT prove UI button wiring.
Linux needs DISPLAY, tauri-driver, WebKitWebDriver, and psutil. Windows needs an
interactive desktop, WebView2, and Python playwright (no browser download).
Release WebView2 CDP refusal is a hard failure, never a source-build fallback.
All output is private evidence. No environment, auth state, or DOM dump is saved.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

OLD = "0.1.806-beta"
NEW = "0.1.807-beta"
OLD_BACKEND = "2026.9.2"
NEW_BACKEND = "2026.9.3"
MANIFEST = "https://github.com/unslothai/unsloth/releases/latest/download/latest.json"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def redact(text):
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", str(text))
    text = re.sub(r"(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+",
                  r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:hf_|sk-)[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(r"https?://[^\s]+", "[URL]", text)


def require_target(value):
    if not value or value.get("version") != NEW or value.get("pypi_version") != NEW_BACKEND:
        raise RuntimeError("Refusing target other than shell 0.1.807-beta/backend 2026.9.3")


def request(url, method="GET", payload=None, timeout=15):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Driver:
    def __init__(self, args):
        self.args = args
        self.art = args.artifacts.resolve()
        self.art.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.wall_started = time.time()
        self.deadline = self.started + args.timeout
        self.env = os.environ.copy()
        # Native 806 uses dirs::home_dir()/.unsloth/studio, not this override.
        self.env.pop("UNSLOTH_STUDIO_HOME", None)
        self.process = None
        self.session = None
        self.page = None
        self.browser = None
        self.playwright = None
        self.base = None
        self.sequence = 0
        self.phase = "preflight"
        self.events = (self.art / "progress.jsonl").open("w", encoding="utf-8")

    def record(self, event, **fields):
        record = {"at": now(), "elapsed_seconds": round(time.monotonic()-self.started, 3),
                  "phase": self.phase, "event": event, **fields}
        self.events.write(json.dumps(record, ensure_ascii=True) + "\n")
        self.events.flush()
        print(f"[{record['at']}] {self.phase}: {event}", flush=True)
        if event == "FAIL":
            # Keep the exception in the primary captured log even if an artifact
            # collector accidentally omits progress.jsonl. Never dump traceback
            # locals, environments, auth responses, or process command lines.
            detail = {key: redact(fields.get(key, ""))[:2000]
                      for key in ("error_type", "error")}
            print(json.dumps(detail, ensure_ascii=True), flush=True)

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Overall timeout in {self.phase}")
        return remaining

    def wait(self, probe, description, budget=None):
        end = min(self.deadline, time.monotonic() + (budget or self.args.timeout))
        last = None
        while time.monotonic() < end:
            try:
                value = probe()
                if value:
                    return value
            except Exception as error:
                last = f"{type(error).__name__}: {redact(error)[:1500]}"
            time.sleep(1)
        raise TimeoutError(f"Timed out: {description}; last exception={last}")

    def wd(self, method, path, payload=None):
        result = request(self.base + path, method, payload, min(30, self.remaining()))
        value = result.get("value")
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError("WebDriver: " + redact(value.get("message", value["error"])))
        return value

    def evaluate(self, expression):
        if os.name == "nt":
            return self.page.evaluate(expression)
        return self.wd("POST", f"/session/{self.session}/execute/sync",
                       {"script": "return (" + expression + ");", "args": []})

    def begin(self, expression):
        self.sequence += 1
        key = f"__upgrade_probe_{self.sequence}"
        self.evaluate("(() => { window[" + json.dumps(key) + "]={done:false}; "
                      "Promise.resolve().then(() => (" + expression + "))"
                      ".then(value => window[" + json.dumps(key) + "]={done:true,value},"
                      "error => window[" + json.dumps(key) + "]={done:true,error:String(error)});"
                      "return true; })()")
        return key

    def finish(self, key):
        while True:
            self.remaining()
            state = self.evaluate("window[" + json.dumps(key) + "]")
            if state and state.get("done"):
                break
            time.sleep(0.25)
        if "error" in state:
            raise RuntimeError(redact(state["error"]))
        return state.get("value")

    def invoke(self, command, payload=None):
        expression = "window.__TAURI__.core.invoke(" + json.dumps(command)
        expression += "," + json.dumps(payload or {}) + ")"
        return self.finish(self.begin(expression))

    def attach_windows(self):
        # Distinguish a dead launch/single-instance exit from a live app whose
        # release WebView2 cannot expose CDP. Never log process environment.
        if self.phase == "launch-806" and self.process.poll() is not None:
            raise RuntimeError(f"Release app exited before CDP attach: exit={self.process.returncode}")
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(
                self.base, timeout=min(10000, self.remaining()*1000))
        except Exception as error:
            raise RuntimeError("CDP discovery/connect failed: " + redact(error)[:1500]) from None
        for context in self.browser.contexts:
            for page in context.pages:
                try:
                    if page.evaluate("Boolean(window.__TAURI__?.core)"):
                        if self.phase == "verify-807":
                            version = page.evaluate("window.__TAURI__.core.invoke('plugin:app|version')")
                            if version != NEW:
                                continue
                        self.page = page
                        self.page.set_default_timeout(10000)
                        return True
                except Exception:
                    pass
        return False

    def new_linux_session(self):
        created = self.wd("POST", "/session", {"capabilities": {"alwaysMatch": {
            "browserName": "wry", "tauri:options": {"application": str(self.args.app)}}}})
        self.session = created["sessionId"]
        return True

    def launch(self):
        self.phase = "launch-806"
        if os.name == "nt":
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            port = free_port()
            self.base = f"http://127.0.0.1:{port}"
            self.env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
                f"--remote-debugging-port={port} --remote-debugging-address=127.0.0.1")
            self.process = subprocess.Popen([str(self.args.app)], env=self.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.wait(self.attach_windows, "release WebView2 CDP (no debug-build fallback)", 120)
        else:
            import psutil  # Required later for narrowly scoped post-relaunch reattachment.
            del psutil
            tauri = shutil.which("tauri-driver")
            native = shutil.which("WebKitWebDriver")
            if not tauri or not native or not os.environ.get("DISPLAY"):
                raise RuntimeError("Linux requires DISPLAY, tauri-driver and WebKitWebDriver")
            self.base = f"http://127.0.0.1:{free_port()}"
            # Do not use extract-and-run: self-update needs the real APPIMAGE path.
            self.env.pop("APPIMAGE_EXTRACT_AND_RUN", None)
            self.env["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
            self.process = subprocess.Popen([tauri, "--port", self.base.rsplit(":", 1)[1],
                "--native-port", str(free_port()), "--native-driver", native], env=self.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            self.wait(lambda: self.wd("GET", "/status"), "tauri-driver startup", 30)
            self.new_linux_session()
        self.wait(lambda: self.evaluate("Boolean(window.__TAURI__?.core)"), "native bridge", 60)
        self.record("release-webview-attached", transport="CDP" if os.name == "nt" else "WebDriver")

    def screenshot(self, label):
        # Mask all editable values and bootstrap/credential panels. Never save DOM,
        # cookies, localStorage, backend owner tokens, or auth responses.
        self.evaluate("""(() => {
          const style=document.createElement('style'); style.id='upgrade-evidence-mask';
          style.textContent='input,textarea,[contenteditable=true],pre,code {visibility:hidden!important}';
          document.head.appendChild(style);
          document.querySelectorAll('body *').forEach(el => {
            if (el.children.length === 0 && /password|token|api.?key|secret|@/i.test(el.textContent||'')) {
              const box=el.closest('form')||el.parentElement;
              if(box && box!==document.body) {box.dataset.upgradeMasked='1'; box.style.visibility='hidden';}
            }
          }); return true;
        })()""")
        try:
            path = self.art / (label + ".png")
            if os.name == "nt":
                self.page.screenshot(path=str(path))
            else:
                data = self.wd("GET", f"/session/{self.session}/screenshot")
                path.write_bytes(base64.b64decode(data))
            self.record("screenshot", label=label, credentials_masked=True)
        finally:
            self.evaluate("""(() => {
              document.getElementById('upgrade-evidence-mask')?.remove();
              document.querySelectorAll('[data-upgrade-masked]').forEach(el => {
                el.style.removeProperty('visibility'); delete el.dataset.upgradeMasked;
              }); return true;
            })()""")

    def backend_version(self):
        python = self.args.studio_home / "unsloth_studio" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python")
        result = subprocess.run([str(python), "-I", "-c",
            "import importlib.metadata; print(importlib.metadata.version('unsloth'))"],
            capture_output=True, text=True, timeout=min(30, self.remaining()), env=self.env)
        if result.returncode:
            raise RuntimeError("Real managed backend package version probe failed")
        return result.stdout.strip()

    def health(self):
        # Anonymous health deliberately omits version fingerprints. CalVer comes
        # from managed package metadata, NOT studio_version (a separate version).
        value = request("http://127.0.0.1:8888/api/health", timeout=min(10, self.remaining()))
        return (value.get("status") == "healthy"
                and value.get("service") == "Unsloth UI Backend")

    def backend_port_closed(self):
        # Require an actual refused connection, not an HTTP error/slow health
        # response, before installing. Subsequent health cannot be the old server
        # continuously surviving the update.
        with socket.socket() as sock:
            sock.settimeout(min(2, self.remaining()))
            try:
                sock.connect(("127.0.0.1", 8888))
            except ConnectionRefusedError:
                return True
            except OSError:
                return False
        return False

    def verify(self, shell, backend):
        actual = self.invoke("plugin:app|version")
        if actual != shell:
            raise RuntimeError(f"Wrong running shell: {actual!r}; expected {shell}")
        if self.backend_version() != backend:
            raise RuntimeError("Wrong installed managed backend version")
        self.wait(self.health, "real backend anonymous healthy response")
        self.record("versions-verified", shell=shell, backend=backend,
                    evidence="native app version + managed package metadata",
                    live_health="anonymous status/service; no version assertion from HTTP")

    def progress(self):
        # Events originate in the real staged installer/native downloader. Drain
        # regularly so multi-GB installations retain timing rather than only totals.
        entries = self.evaluate("window.__upgradeEvents ? window.__upgradeEvents.splice(0) : []")
        for entry in entries or []:
            self.record("native-progress", native_at=entry["at"],
                        kind=entry["kind"], message=redact(entry["message"]))

    def prepare(self):
        self.phase = "native-check"
        metadata = self.invoke("check_desktop_update")
        require_target({"version": metadata and metadata.get("version"),
                        "pypi_version": (metadata or {}).get("rawJson", {}).get("pypi_version")})
        if metadata.get("currentVersion") != OLD:
            raise RuntimeError("Updater metadata does not identify current shell806")
        if self.invoke("desktop_update_policy").get("mode") != "in_app":
            raise RuntimeError("This installed package cannot update in-app")
        self.record("target-accepted", shell=NEW, backend=NEW_BACKEND, mode="IPC-driven staged fast path")
        self.finish(self.begin("""(async () => {
          window.__upgradeEvents=[];
          for(const kind of ['stage-progress','stage-complete','stage-failed','desktop-update-download']) {
            await window.__TAURI__.event.listen(kind, e => window.__upgradeEvents.push({
              at:new Date().toISOString(),kind,message:typeof e.payload==='string'?e.payload:JSON.stringify(e.payload)
            }));
          } return true;
        })()"""))
        self.phase = "stage-backend-and-download-shell"
        stage = self.begin("window.__TAURI__.core.invoke('start_staged_update')")
        download = self.begin("window.__TAURI__.core.invoke('download_desktop_update')")
        last = None
        while True:
            self.remaining()
            self.progress()
            staged = self.invoke("staged_update_status")
            bundle = self.invoke("desktop_update_bundle_status")
            state = {"staged": staged, "bundle": bundle}
            if state != last:
                self.record("preparation-status", **state)
                last = state
            for key in (stage, download):
                result = self.evaluate("window[" + json.dumps(key) + "]")
                if result and result.get("error"):
                    raise RuntimeError(redact(result["error"]))
            if staged.get("state") == "ready" and bundle.get("downloaded"):
                if (staged.get("shellVersion") != NEW or staged.get("backendVersion") != NEW_BACKEND
                        or bundle.get("version") != NEW):
                    raise RuntimeError("Prepared bundle/stage target mismatch")
                break
            time.sleep(2)
        self.finish(stage)
        self.finish(download)
        self.progress()
        self.record("staged-backend-and-signed-shell-ready")

    def linux_reconnect(self):
        # WebKit's automation session is attached to the old PID. Native relaunch
        # starts a non-automated replacement; first prove that replacement brought
        # up a healthy backend after the old port was confirmed closed, then
        # terminate ONLY our replacement shell and reopen the installed AppImage.
        # Backend807 is verified from managed metadata, never anonymous HTTP.
        import psutil
        self.wait(self.health, "backend healthy again after confirmed old-server stop")
        self.record("relaunched-backend-healthy")
        actual_backend = self.backend_version()
        self.record("relaunched-backend-metadata", backend=redact(actual_backend))
        if actual_backend != NEW_BACKEND:
            raise RuntimeError(f"Relaunch backend metadata mismatch: {actual_backend!r}")
        matches = []
        candidates = []
        inspection_errors = {}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name_matches = "unsloth" in proc.info["name"].lower()
                new_pid = proc.pid not in self.pre_restart_pids
                if not name_matches and not new_pid:
                    continue
                appimage = proc.environ().get("APPIMAGE")
                path_matches = bool(appimage) and Path(appimage).resolve() == self.args.app
                if name_matches or path_matches:
                    # Only numeric/boolean identity facts, not environment values.
                    candidates.append({"pid": proc.pid, "name_matches": name_matches,
                                       "absent_before_restart": new_pid,
                                       "appimage_present": bool(appimage),
                                       "appimage_matches": path_matches})
                if new_pid and name_matches and path_matches:
                    matches.append(proc)
            except (psutil.Error, OSError) as error:
                kind = type(error).__name__
                inspection_errors[kind] = inspection_errors.get(kind, 0) + 1
        self.record("relaunch-process-inspection", candidates=candidates,
                    inspection_errors=inspection_errors)
        if not matches:
            raise RuntimeError("Cannot prove/identify the native relaunched AppImage process; "
                               + json.dumps({"candidates": candidates,
                                             "inspection_errors": inspection_errors}))
        self.record("native-relaunch-observed", replacement_pids=[p.pid for p in matches])
        for proc in matches:
            proc.terminate()
        _, alive = psutil.wait_procs(matches, timeout=20)
        if alive:
            raise RuntimeError("Replacement shell did not stop for WebDriver reattachment")
        try:
            self.wd("DELETE", f"/session/{self.session}")
        except Exception:
            pass
        self.record("verification-reopen", reason="WebKit session cannot attach to a relaunched PID")
        self.new_linux_session()

    def run(self):
        expected_home = (Path.home() / ".unsloth/studio").resolve()
        if self.args.studio_home.resolve() != expected_home:
            raise RuntimeError("--studio-home must equal the native user's ~/.unsloth/studio")
        if not self.args.app.is_file():
            raise RuntimeError("--app must be an installed release executable/AppImage")
        require_target(request(MANIFEST, timeout=min(30, self.remaining())))
        if self.backend_version() != OLD_BACKEND:
            raise RuntimeError("Preinstalled managed backend must be 2026.9.2")
        self.record("preflight-passed", mode="IPC-driven", target=NEW)
        with self.args.app.open("rb") as handle:
            before_hash = hashlib.file_digest(handle, "sha256").hexdigest()
        self.launch()
        self.verify(OLD, OLD_BACKEND)
        self.screenshot("before-806")
        self.prepare()
        self.phase = "stop-install-relaunch"
        # Recheck external latest to refuse a moving target before mutation. The
        # native pending update itself remains the previously checked807 object.
        require_target(request(MANIFEST, timeout=min(30, self.remaining())))
        if not self.invoke("desktop_update_cleanup_armed"):
            self.invoke("resume_desktop_update_cleanup")
        self.invoke("stop_server")
        self.wait(self.backend_port_closed, "old backend TCP port closed", 30)
        self.record("backend-stopped", evidence="native stop_server + TCP connection refused")
        self.invoke("set_renderer_activity", {"kind": "shell_update", "active": True})
        self.record("install-started")
        # Windows NSIS may exit this process while install is in progress. Losing
        # the bridge is NOT proof of success; subsequent version/health checks are.
        try:
            self.invoke("install_desktop_update")
            self.record("install-returned")
            self.invoke("set_renderer_activity", {"kind": "shell_update", "active": False})
            self.invoke("mark_in_app_relaunch")
            if os.name != "nt":
                import psutil
                # Snapshot before restart IPC. psutil create_time and time.time()
                # need not agree at subsecond precision (run2 rejected both new
                # AppImage PIDs). Existing PIDs, including same-PID exec, remain
                # excluded: only a distinct matching shell proves this handoff.
                self.pre_restart_pids = set(psutil.pids())
                self.record("pre-restart-process-snapshot", count=len(self.pre_restart_pids))
            self.begin("window.__TAURI__.core.invoke('plugin:process|restart')")
            self.record("native-relaunch-requested")
        except Exception:
            if os.name != "nt":
                raise
            self.record("windows-install-bridge-disconnected", proof_pending=True)
        self.phase = "verify-807"
        if os.name == "nt":
            self.wait(self.attach_windows, "updated release WebView2 CDP reattachment", 180)
        else:
            self.linux_reconnect()
        self.verify(NEW, NEW_BACKEND)
        with self.args.app.open("rb") as handle:
            after_hash = hashlib.file_digest(handle, "sha256").hexdigest()
        if before_hash == after_hash:
            raise RuntimeError("Installed shell bytes did not change")
        self.screenshot("after-807")
        self.record("PASS", mode="IPC-driven", before_sha256=before_hash, after_sha256=after_hash)

    def close(self):
        # Do not collect raw process output: the parent retains native diagnostic
        # logs privately and handles publication/redaction separately.
        if self.playwright:
            self.playwright.stop()
        if self.session:
            try:
                self.wd("DELETE", f"/session/{self.session}")
            except Exception:
                pass
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.events.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--studio-home", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if sys.platform not in ("linux", "win32") or args.timeout <= 0:
        parser.error("Only Linux/Windows and a positive timeout are supported")
    args.app = args.app.resolve()
    driver = Driver(args)
    try:
        driver.run()
    except Exception as error:
        driver.record("FAIL", error_type=type(error).__name__, error=redact(error))
        return 1
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
