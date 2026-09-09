# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Install official 806, exercise its native updater, retain bounded timing evidence.

Run only on disposable GitHub hosted runners. No local/source Studio installation,
no fake backend, no updater manifest rewriting, and no automatic cache cleanup.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HERE = Path(__file__).resolve().parent
OLD = "v0.1.806-beta"
NEW = "v0.1.807-beta"
ASSETS = {
    "Darwin": ("Unsloth-Desktop-MacOS.dmg", "ec9d320140fe523728e5f029df17f62b8a3b8b2200081628e573fd60c6bcd5ee"),
    "Linux": ("Unsloth-Desktop-Linux.AppImage", "426f6ad066b1bb191c1497f094b5d9cb6afd07541499bd8e26b31da5e896c816"),
    "Windows": ("Unsloth-Desktop-Windows.exe", "a262e5312b9b812399be1baf39dd9b45ce44101bb6fecc687cbe22d80eba855d"),
}


def json_url(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class Run:
    def __init__(self, root):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw = self.root / "private-raw"
        self.raw.mkdir(exist_ok=True)
        self.public = self.root / "public"
        self.public.mkdir(exist_ok=True)
        self.studio = Path.home() / ".unsloth" / "studio"
        timing_path = self.public / "timings.json"
        self.timings = json.loads(timing_path.read_text()) if timing_path.is_file() else []
        self.env = dict(os.environ)
        # A setup action's UV_CACHE_DIR would turn the experiment into an explicit
        # override. Let the official installer choose its normal default instead.
        for name in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "UNSLOTH_STUDIO_HOME", "STUDIO_HOME",
                     "UV_CONSTRAINT", "UV_OVERRIDE", "UNSLOTH_NO_TORCH", "UV_NO_CACHE"):
            self.env.pop(name, None)
        self.env.update(UNSLOTH_SKIP_AUTOSTART="1", UNSLOTH_VERBOSE="1",
                        PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                        LANG="en_US.UTF-8")
        self.app = None

    def command(self, label, command, *, env=None, cwd=None, timeout=1800):
        start = time.monotonic()
        log = self.raw / f"{label}.log"
        print(f"[{now()}] BEGIN {label}", flush=True)
        with log.open("w", encoding="utf-8") as handle:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     env=env or self.env, cwd=cwd)
            # A timer bounds commands whose output iterator would otherwise hang.
            timer = threading.Timer(timeout, child.kill)
            timer.start()
            try:
                for line in child.stdout:
                    # Do not echo raw installation output into Actions: it can contain
                    # a bootstrap password. Native/installer logs are redacted later.
                    handle.write(f"[{now()}] {line}")
                    handle.flush()
                code = child.wait()
            finally:
                timer.cancel()
        elapsed = round(time.monotonic() - start, 3)
        self.timings.append({"phase": label, "seconds": elapsed, "exit_code": code})
        print(f"[{now()}] END {label}: exit={code}, seconds={elapsed}", flush=True)
        self.save_timings()
        if code:
            raise RuntimeError(f"{label} exited {code}; see sanitized {label}.log")
        return log

    def save_timings(self):
        (self.public / "timings.json").write_text(json.dumps(self.timings, indent=2) + "\n")

    def python(self):
        return self.studio / "unsloth_studio" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def packages(self):
        py = self.python()
        if not py.exists():
            return {}
        code = "import importlib.metadata as m,json; names=['unsloth','unsloth-zoo','torch','torchao','transformers','mlx','uv']; out={};\nfor n in names:\n try: out[n]=m.version(n)\n except m.PackageNotFoundError: pass\nprint(json.dumps(out))"
        result = subprocess.run([str(py), "-I", "-c", code], env=self.env,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            return {"probe_exit": result.returncode}
        return json.loads(result.stdout)

    def find_uv(self):
        candidates = [shutil.which("uv", path=self.env["PATH"]),
                      str(Path.home() / ".local/bin/uv"),
                      str(Path.home() / ".local/bin/uv.exe"),
                      str(self.studio / "unsloth_studio/Scripts/uv.exe")]
        return next((c for c in candidates if c and Path(c).is_file()), None)

    def snapshot(self, label):
        uv = self.find_uv()
        default = None
        if uv:
            result = subprocess.run([uv, "cache", "dir"], env=self.env,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                default = Path(result.stdout.strip())
        marker = self.studio / "cache/uv-cache-dir"
        selected = marker.read_text(encoding="utf-8-sig").strip() if marker.is_file() else None
        paths = {"studio_uv": self.studio / "cache/uv"}
        if default is not None:
            paths["global_uv"] = default
        cache = {}
        for name, path in paths.items():
            files = {}
            if path.is_dir():
                for directory, _, names in os.walk(path):
                    for filename in names:
                        full = Path(directory) / filename
                        try:
                            stat = full.stat()
                            files[str(full.relative_to(path))] = stat.st_size
                        except OSError:
                            pass
            cache[name] = {"path": str(path), "files": len(files),
                           "logical_bytes": sum(files.values()), "inventory": files}
        data = {"at": now(), "packages": self.packages(), "cache_marker": selected,
                "updater_parent_uv_cache_dir": "unset", "caches": cache}
        (self.raw / f"cache-{label}.json").write_text(json.dumps(data) + "\n")
        summary = {**data, "caches": {k: {a: b for a, b in v.items() if a != "inventory"}
                                      for k, v in cache.items()}}
        (self.public / f"cache-{label}.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"[{now()}] cache snapshot {label}: " + json.dumps(summary), flush=True)

    def install(self):
        if self.studio.exists():
            raise RuntimeError("Refusing a non-fresh runner: Studio root already exists")
        latest = json_url("https://github.com/unslothai/unsloth/releases/latest/download/latest.json")
        assert latest["version"] == NEW[1:] and latest["pypi_version"] == "2026.9.3", latest
        for tag, backend in ((OLD, "2026.9.2"), (NEW, "2026.9.3")):
            metadata = json_url(f"https://github.com/unslothai/unsloth/releases/download/{tag}/latest.json")
            assert metadata["version"] == tag[1:] and metadata["pypi_version"] == backend
            (self.public / f"{tag}-latest.json").write_text(json.dumps(metadata, indent=2) + "\n")
        asset, digest = ASSETS[platform.system()]
        target = self.root / asset
        started = time.monotonic()
        with urllib.request.urlopen(f"https://github.com/unslothai/unsloth/releases/download/{OLD}/{asset}", timeout=120) as src:
            with target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
        with target.open("rb") as src:
            assert hashlib.file_digest(src, "sha256").hexdigest() == digest
        self.timings.append({"phase": "download-official-806", "seconds": round(time.monotonic()-started, 3),
                             "bytes": target.stat().st_size, "sha256": digest, "exit_code": 0})
        system = platform.system()
        if system == "Darwin":
            mount = self.root / "mount"
            mount.mkdir()
            self.command("mount-806", ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(target)], timeout=120)
            try:
                bundle = next(mount.glob("*.app"))
                app_dir = Path("/Applications") / bundle.name
                if app_dir.exists():
                    raise RuntimeError(f"Existing app at {app_dir}")
                self.command("install-806-app", ["ditto", str(bundle), str(app_dir)], timeout=120)
            finally:
                self.command("unmount-806", ["hdiutil", "detach", str(mount)], timeout=120)
            plist = plistlib.loads((app_dir / "Contents/Info.plist").read_bytes())
            assert plist["CFBundleShortVersionString"] == OLD[1:], plist["CFBundleShortVersionString"]
            self.app = app_dir / "Contents/MacOS" / plist["CFBundleExecutable"]
            installer = app_dir / "Contents/Resources/install.sh"
            if not installer.is_file():
                installer = next((app_dir / "Contents/Resources").rglob("install.sh"))
            bootstrap = ["bash", str(installer), "--tauri"]
        elif system == "Windows":
            self.command("install-806-app", [str(target), "/S"], timeout=300)
            roots = [Path(os.environ["LOCALAPPDATA"]) / "Unsloth Studio (Desktop)",
                     Path(os.environ["LOCALAPPDATA"]) / "Programs",
                     Path(os.environ["LOCALAPPDATA"]) / "Unsloth"]
            candidates = [p for r in roots if r.exists() for p in r.rglob("install.ps1") if "unsloth" in str(p).lower()]
            if not candidates:
                raise RuntimeError("Installed official app contains no install.ps1")
            installer = candidates[0]
            exes = [p for p in installer.parent.rglob("*.exe") if p.name.lower() in ("unsloth.exe", "unsloth-studio.exe")]
            if not exes:
                raise RuntimeError(f"Could not locate native app beside {installer}")
            self.app = exes[0]
            bootstrap = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(installer), "--tauri"]
        else:
            target.chmod(0o755)
            self.command("extract-806-resource", [str(target), "--appimage-extract"], cwd=self.root, timeout=180)
            installer = next((self.root / "squashfs-root").rglob("install.sh"))
            self.app = target
            bootstrap = ["bash", str(installer), "--tauri"]
        self.snapshot("before-install")
        # The old app's minimum is not an exact pin. Constrain ONLY bootstrap so
        # today's installer cannot silently start the test at backend 2026.9.3.
        constraints = self.root / "baseline-constraints.txt"
        constraints.write_text("unsloth==2026.9.2\nunsloth-zoo==2026.9.1\n")
        env = {**self.env, "UV_CONSTRAINT": str(constraints),
               "PIP_CONSTRAINT": str(constraints), "UNSLOTH_DESKTOP_BACKEND_VERSION": "2026.9.2",
               "UNSLOTH_PYTHON": sys.executable}
        self.command("bootstrap-806-backend", bootstrap, env=env, timeout=2400)
        versions = self.packages()
        assert versions.get("unsloth") == "2026.9.2", versions
        # uv bootstrap may put uv in ~/.local/bin, which a separately launched
        # native app needs to find just as an installed desktop launcher does.
        local_bin = str(Path.home() / ".local/bin")
        self.env["PATH"] = local_bin + os.pathsep + self.env["PATH"]
        self.snapshot("installed-806")
        (self.public / "provenance.json").write_text(json.dumps({
            "old": OLD, "new": NEW, "old_asset_sha256": digest,
            "old_source": "e055f92206564591251046d1f50f936265298583",
            "new_source": "cedbb58e4a49befe12d4f28f385abc4393c763e5",
            "app": str(self.app), "studio_home": str(self.studio),
            "bootstrap": "806 bundled installer --tauri; initial package versions constrained",
            "packages_before_native_launch": versions,
            "no_torch": False, "source_rebuilt": False,
        }, indent=2) + "\n")

    def upgrade(self):
        script = HERE / ("mac_driver.py" if platform.system() == "Darwin" else "native_driver.py")
        driver_artifacts = self.raw / "native-driver"
        driver_artifacts.mkdir(exist_ok=True)
        try:
            self.command("native-806-to-807", [sys.executable, str(script), "--app", str(self.app),
                         "--artifacts", str(driver_artifacts), "--studio-home", str(self.studio),
                         "--timeout", "1800"], timeout=1900)
        finally:
            self.snapshot("after-upgrade")
        assert self.packages().get("unsloth") == "2026.9.3", self.packages()

    def collect(self):
        # Small explicit allowlist. Never copy auth/, databases, caches or whole homes.
        for parent, pattern in ((self.studio / "logs", "update-*.log"),
                                (self.studio / "logs", "install-*.log"),
                                (self.studio, "tauri.log")):
            if parent.exists():
                for path in parent.glob(pattern):
                    shutil.copy2(path, self.raw / ("native-" + path.name))
        secrets = []
        for path in (self.studio / "auth/.bootstrap_password", self.studio / ".bootstrap_password"):
            if path.is_file():
                secret = path.read_text(errors="replace").strip()
                if secret:
                    secrets.append(secret)
        for path in self.raw.rglob("*"):
            if not path.is_file() or path.suffix not in (".log", ".txt", ".json", ".jsonl"):
                continue
            if path.name.startswith("cache-"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            # Filter sensitive complete lines, including screen/body dumps. Do not
            # publish native screenshots automatically until they are inspected.
            text = "\n".join(
                "[REDACTED sensitive line]" if re.search(
                    r"(?i)(password|authorization|bearer |access_token|refresh_token|api[_ -]?key|eyJ[A-Za-z0-9_-]{20,})", line
                ) else line for line in text.splitlines()
            ) + "\n"
            out = self.public / path.relative_to(self.raw)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
        self.save_timings()
        # Receipt timestamps from native phase logs, rather than Actions step times.
        events = []
        for path in self.public.glob("native-update-*.log"):
            for line in path.read_text(errors="replace").splitlines():
                match = re.match(r"\[(\d{13})\]\[[^]]+\]\s*(.*)", line)
                if match and ("[TAURI:STEP]" in match[2] or re.search(r"\b\d+/\d+\b|pre-installed|deps\s+installed|Staged Unsloth", match[2])):
                    events.append({"timestamp_ms": int(match[1]), "message": match[2], "file": path.name})
        events.sort(key=lambda e: e["timestamp_ms"])
        for i, event in enumerate(events[:-1]):
            event["seconds_to_next_marker"] = round((events[i+1]["timestamp_ms"]-event["timestamp_ms"])/1000, 3)
        (self.public / "native-phase-timings.json").write_text(json.dumps(events, indent=2)+"\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This harness installs software: run only on disposable Actions runners")
    if os.environ.get("GITHUB_REPOSITORY") != "wasimysaid/unsloth":
        raise SystemExit("Fork-only harness: upstream execution refused")
    run = Run(args.artifacts)
    try:
        if not args.collect_only:
            run.install()
            run.upgrade()
    finally:
        run.collect()


if __name__ == "__main__":
    main()
