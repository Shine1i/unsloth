#!/usr/bin/env python3
"""Live PR #8299 credential persistence probe. Never prints secrets."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from studio_test_kit.auth import ProviderSeed, StudioAuth, login, seed_init_script
from studio_test_kit.ui import open_chat, send_prompt, wait_for_stream

MODEL = "gpt-4o-mini"
NEW_PASSWORD = "UnslothStudioLiveCI2026!"


def passed(message: str) -> None:
    print(f"PASS {message}", flush=True)


def fail(message: str) -> None:
    print(f"FAIL {message}", flush=True)
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
    fail(f"could not find installed unsloth CLI under {home}")


def read_bootstrap_password(home: Path, log_path: Path) -> str | None:
    for rel in ("auth/.bootstrap_password", ".bootstrap_password"):
        try:
            value = (home / rel).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(
        r"(?i)(?:bootstrap|initial|generated)\s*password(?:\s+is)?\s*[:=]?\s*(\S+)",
        text,
    )
    return match.group(1).strip().strip(".,") if match else None


def wait_for_health(base_url: str, timeout_s: int = 240) -> str:
    deadline = time.time() + timeout_s
    for _ in iter(int, 1):
        for path in ("/healthz", "/api/health"):
            try:
                with urllib.request.urlopen(f"{base_url}{path}", timeout=3) as response:
                    if response.status < 500:
                        return path
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
        if time.time() >= deadline:
            fail("Studio failed to become healthy")
        time.sleep(2)


def studio_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(home)
    return env


def start_studio(home: Path, log_path: Path, port: int) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    kwargs: dict = {
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "env": studio_env(home),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [str(find_unsloth_bin(home)), "studio", "-H", "127.0.0.1", "-p", str(port)],
        **kwargs,
    )
    log.close()
    return proc


def stop_studio(proc: subprocess.Popen | None) -> None:
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
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()


def bearer(auth: StudioAuth) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.access_token}"}


async def authenticate(base_url: str, password: str) -> tuple[StudioAuth, str]:
    auth = await login(base_url, "unsloth", password)
    if not auth.must_change_password:
        return auth, password
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base_url}/api/auth/change-password",
            headers=bearer(auth),
            json={"current_password": password, "new_password": NEW_PASSWORD},
        )
        response.raise_for_status()
        body = response.json()
    auth.access_token = body["access_token"]
    auth.refresh_token = body.get("refresh_token", "")
    auth.must_change_password = False
    passed("Studio first-boot password change succeeded")
    return auth, NEW_PASSWORD


def encrypt_provider_key(public_key_pem: str, plaintext: str) -> str:
    key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    encrypted = key.encrypt(
        plaintext.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(encrypted).decode("ascii")


async def save_credentials(
    base_url: str,
    auth: StudioAuth,
    hf_token: str,
    openai_key: str,
    artifact_dir: Path,
) -> str:
    """Drive the real Settings UI to save both credentials."""
    init_script = seed_init_script(
        auth,
        [],
        connections_enabled=True,
        extra_local_storage={"unsloth_onboarding_done": "true"},
    )
    shots = artifact_dir / "settings-ui"
    shots.mkdir(parents=True, exist_ok=True)
    async with open_chat(
        base_url,
        init_scripts=[init_script],
        video_dir=artifact_dir / "video",
        video_name="settings-credential-entry",
        browser_name=os.environ.get("STUDIO_BROWSER", "chromium"),
    ) as studio_page:
        page = studio_page.page
        profile = page.locator("button").filter(has_text="Unsloth").last
        await profile.click(timeout=30_000)
        await page.get_by_role("menuitem", name=re.compile(r"^Settings")).click()
        dialog = page.get_by_role("dialog")
        await dialog.wait_for(state="visible", timeout=30_000)

        hf_input = dialog.locator('input[name="hf-token"]')
        await hf_input.fill(hf_token)
        await hf_input.press("Tab")
        await page.wait_for_timeout(1_500)
        await studio_page.screenshot(shots / "01_hf_token_saved.png")

        await dialog.get_by_role("button", name="Connections", exact=True).click()
        await dialog.get_by_role(
            "button", name=re.compile(r"^Add connection")
        ).click()
        await dialog.locator("#provider-preset").click()
        await page.get_by_role("option", name="OpenAI", exact=True).click()
        await dialog.locator("#provider-api-key").fill(openai_key)
        manual_models = dialog.locator("#provider-manual-models")
        await manual_models.wait_for(state="visible", timeout=30_000)
        await manual_models.fill(MODEL)
        await studio_page.screenshot(shots / "02_openai_connection_filled.png")
        await dialog.get_by_role("button", name="Add connection", exact=True).click()
        await dialog.get_by_text(MODEL, exact=True).wait_for(timeout=30_000)
        await studio_page.screenshot(shots / "03_openai_connection_saved.png")

    async with httpx.AsyncClient(timeout=30) as client:
        hf_response = await client.get(
            f"{base_url}/api/settings/hugging-face-token", headers=bearer(auth)
        )
        hf_response.raise_for_status()
        if hf_response.json().get("token") != hf_token:
            fail("Settings UI did not persist the Hugging Face token")
        providers_response = await client.get(
            f"{base_url}/api/providers/", headers=bearer(auth)
        )
        providers_response.raise_for_status()
        providers = providers_response.json()
    provider = next(
        (item for item in providers if item.get("provider_type") == "openai"),
        None,
    )
    if not provider or provider.get("has_api_key") is not True:
        fail("Settings UI did not persist the OpenAI connection and key")
    serialized = json.dumps(provider)
    if openai_key in serialized or "encrypted_api_key" in provider or "api_key" in provider:
        fail("provider response exposed credential material")
    passed("HF token and OpenAI connection were saved through the live Settings UI")
    return str(provider["id"])


async def verify_hf_live(hf_token: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            "https://huggingface.co/api/whoami-v2",
            headers={"Authorization": f"Bearer {hf_token}"},
        )
    if response.status_code != 200:
        fail(f"Hugging Face live validation failed with status {response.status_code}")
    passed("persisted Hugging Face token authenticated against the live Hub")


async def verify_saved_credentials(
    base_url: str, auth: StudioAuth, provider_id: str, hf_token: str
) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        hf_response = await client.get(
            f"{base_url}/api/settings/hugging-face-token", headers=bearer(auth)
        )
        hf_response.raise_for_status()
        hf_body = hf_response.json()
        providers_response = await client.get(
            f"{base_url}/api/providers/", headers=bearer(auth)
        )
        providers_response.raise_for_status()
        providers = providers_response.json()
    if hf_body.get("has_token") is not True or hf_body.get("token") != hf_token:
        fail("HF token did not survive Studio restart")
    match = next((row for row in providers if row.get("id") == provider_id), None)
    if not match or match.get("has_api_key") is not True:
        fail("saved provider key did not survive Studio restart")
    if any("api_key" in key and key != "has_api_key" for key in match):
        fail("provider list returned credential material")
    passed("HF and provider credentials survived a real Studio restart")


async def live_chat(
    base_url: str,
    auth: StudioAuth,

    provider_id: str,
    artifact_dir: Path,
    label: str,
    prompt: str,
) -> None:
    init_script = seed_init_script(
        auth,
        [
            ProviderSeed(
                provider_type="openai",
                name="OpenAI Live CI",
                base_url="https://api.openai.com/v1",
                models=[MODEL],
                api_key="",
                id=provider_id,
            )
        ],
        connections_enabled=True,
        extra_local_storage={"unsloth_onboarding_done": "true"},
    )
    async with open_chat(
        base_url,
        init_scripts=[init_script],
        video_dir=artifact_dir / "video",
        video_name=label,
        browser_name=os.environ.get("STUDIO_BROWSER", "chromium"),
    ) as studio_page:
        shots = artifact_dir / label
        shots.mkdir(parents=True, exist_ok=True)
        await studio_page.screenshot(shots / "01_chat_open.png")
        trigger = studio_page.page.get_by_role(
            "button", name=re.compile(r"^\s*Select model\s*$")
        ).first
        await trigger.click(timeout=30_000)
        option = studio_page.page.get_by_role(
            "option", name=re.compile(rf"^\s*{re.escape(MODEL)}\s*$")
        ).first
        await option.click(timeout=30_000)
        await studio_page.screenshot(shots / "02_model_picked.png")
        await send_prompt(studio_page, prompt)
        await studio_page.screenshot(shots / "03_prompt_sent.png")
        await wait_for_stream(studio_page, timeout_ms=120_000)
        await studio_page.screenshot(shots / "04_response.png")
        body_text = await studio_page.page.locator("body").inner_text()
        if "error" in body_text.lower() and "connection" in body_text.lower():
            fail("Studio UI displayed a provider connection error")
    passed(f"real OpenAI chat completed through saved provider ({label})")


def assert_no_plaintext(root: Path, secrets: list[str]) -> None:
    needles = [value.encode("utf-8") for value in secrets]
    checked = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 50_000_000:
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        checked += 1
        if any(needle in blob for needle in needles):
            fail(f"plaintext credential found in persisted file {path.relative_to(root)}")
    if checked == 0:
        fail("no persisted files were available for plaintext inspection")
    passed(f"plaintext credential scan passed across {checked} persisted files")


async def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifact_dir = Path(os.environ["STUDIO_ARTIFACT_DIR"]).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    hf_token = os.environ.get("HF_TOKEN", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not hf_token or not openai_key:
        fail("required repository secrets are unavailable")

    password: str | None = None
    provider_id = ""
    proc: subprocess.Popen | None = None
    try:
        first_port = free_port()
        first_base = f"http://127.0.0.1:{first_port}"
        first_log = artifact_dir / "studio-first.log"
        proc = start_studio(home, first_log, first_port)
        health_path = wait_for_health(first_base)
        passed(f"fresh Studio launch became healthy at {health_path}")
        bootstrap = read_bootstrap_password(home, first_log)
        if not bootstrap:
            fail("could not obtain Studio bootstrap password")
        auth, password = await authenticate(first_base, bootstrap)
        provider_id = await save_credentials(
            first_base, auth, hf_token, openai_key, artifact_dir
        )
        await verify_hf_live(hf_token)
        await live_chat(
            first_base,
            auth,

            provider_id,
            artifact_dir,
            "before-restart",
            "Reply with exactly: credential persistence live check passed.",
        )
        stop_studio(proc)
        proc = None
        assert_no_plaintext(home, [hf_token, openai_key])

        second_port = free_port()
        second_base = f"http://127.0.0.1:{second_port}"
        second_log = artifact_dir / "studio-after-restart.log"
        proc = start_studio(home, second_log, second_port)
        health_path = wait_for_health(second_base)
        passed(f"restarted Studio became healthy at {health_path}")
        auth, _ = await authenticate(second_base, password)
        await verify_saved_credentials(second_base, auth, provider_id, hf_token)
        await live_chat(
            second_base,
            auth,

            provider_id,
            artifact_dir,
            "after-restart",
            "Reply with exactly: saved provider survived restart.",
        )
        stop_studio(proc)
        proc = None
        assert_no_plaintext(home, [hf_token, openai_key])

        assert_no_plaintext(artifact_dir, [hf_token, openai_key])

        (artifact_dir / "summary.json").write_text(
            json.dumps(
                {
                    "install": True,
                    "first_launch": True,
                    "hf_live": True,
                    "provider_chat": True,
                    "restart": True,
                    "hf_persisted": True,
                    "provider_persisted": True,
                    "plaintext_scan": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        passed("all PR #8299 live credential checks completed")
    finally:
        stop_studio(proc)


if __name__ == "__main__":
    asyncio.run(main())
