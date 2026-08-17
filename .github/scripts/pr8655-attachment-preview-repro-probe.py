#!/usr/bin/env python3
"""A/B live Studio proof for unslothai/unsloth#8655."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from playwright.async_api import async_playwright, expect


BASE_REF = "44afbdfc536a5494b0c63e10318aa329c81f3111"


def pass_log(message: str) -> None:
    print(f"PASS {message}", flush=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_unsloth_bin(home: Path) -> Path:
    candidates = (
        home / "bin" / "unsloth",
        home / "bin" / "unsloth.exe",
        home / "unsloth_studio" / "bin" / "unsloth",
        home / "unsloth_studio" / "Scripts" / "unsloth.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(f"could not find isolated unsloth CLI under {home}")


def start_studio(home: Path, log_path: Path, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["UNSLOTH_STUDIO_HOME"] = str(home)
    env["UNSLOTH_SKIP_AUTOSTART"] = "1"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    kwargs: dict = {
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [
            str(find_unsloth_bin(home)),
            "studio",
            "-H",
            "127.0.0.1",
            "-p",
            str(port),
        ],
        **kwargs,
    )
    log_handle.close()
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
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()


def wait_for_health(base_url: str, timeout_s: int = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for path in ("/healthz", "/api/health"):
            try:
                with urllib.request.urlopen(f"{base_url}{path}", timeout=3) as response:
                    if response.status < 500:
                        pass_log(f"Studio health check succeeded at {path}")
                        return
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
        time.sleep(2)
    raise AssertionError("Studio did not become healthy")


def read_bootstrap_password(home: Path, log_path: Path) -> str:
    for path in (home / "auth" / ".bootstrap_password", home / ".bootstrap_password"):
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(
        r"(?i)(?:bootstrap|initial|generated)\s*password(?:\s+is)?\s*[:=]?\s*(\S+)",
        text,
    )
    if not match:
        raise AssertionError("could not read Studio bootstrap password")
    return match.group(1).strip().strip(".,")


def post_json(url: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def login_tokens(base_url: str, password: str) -> dict:
    payload = post_json(
        f"{base_url}/api/auth/login",
        {"username": "unsloth", "password": password},
    )
    if payload.get("must_change_password"):
        new_password = "UnslothStudioPR8655CI!"
        payload = post_json(
            f"{base_url}/api/auth/change-password",
            {"current_password": password, "new_password": new_password},
            payload["access_token"],
        )
    pass_log("Studio API login succeeded")
    return payload


def auth_init_script(tokens: dict) -> str:
    access = json.dumps(tokens["access_token"])
    refresh = json.dumps(tokens.get("refresh_token", ""))
    return f"""
      localStorage.setItem("unsloth_auth_token", {access});
      localStorage.setItem("unsloth_auth_refresh_token", {refresh});
      localStorage.removeItem("unsloth_auth_must_change_password");
    """


def write_pdf(path: Path) -> None:
    stream = (
        b"BT /F1 12 Tf 72 720 Td (PDF first line) Tj "
        b"0 -20 Td (PDF second line) Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    path.write_bytes(output)


def write_docx(path: Path, body_text: str | None = None) -> None:
    body = body_text or (
        "<w:p><w:r><w:t>DOCX first line</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>DOCX second line</w:t></w:r></w:p>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)


def write_odt(path: Path) -> None:
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:text>"
        "<text:p>ODT first line</text:p><text:p>ODT second line</text:p>"
        "</office:text></office:body></office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)


def make_fixtures(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "preview.txt").write_text(
        "Attachment preview fixture\nSecond line stays separate.\n"
        "Third line contains <tags>, ampersands & emoji 🥕.\n",
        encoding="utf-8",
    )
    (directory / "preview.html").write_text(
        "<h1>Fixture heading</h1><p>First paragraph.</p>"
        "<script>ignored()</script><p>Second paragraph.</p>",
        encoding="utf-8",
    )
    (directory / "long-preview.txt").write_text(
        ("bounded preview line\n" * 70_000) + "TAIL-MUST-NOT-RENDER",
        encoding="utf-8",
    )
    write_pdf(directory / "preview.pdf")
    write_docx(directory / "preview.docx")
    write_docx(directory / "oversized.docx", "x" * 10_600_000)
    write_odt(directory / "preview.odt")
    return {path.name: path for path in directory.iterdir() if path.is_file()}


async def attach(page, path: Path):
    await page.get_by_role("button", name="Tools and attachments").click()
    async with page.expect_file_chooser() as chooser_info:
        await page.get_by_role("menuitem", name="Add photos & files").click()
    chooser = await chooser_info.value
    await chooser.set_files(str(path))
    tile = page.get_by_role("button", name=re.compile(re.escape(path.name)))
    await expect(tile).to_be_visible(timeout=20_000)
    return tile


async def close_and_remove(page, tile) -> None:
    await page.keyboard.press("Escape")
    await expect(page.get_by_role("dialog")).to_have_count(0)
    await page.get_by_role("button", name="Remove file").click()
    await expect(tile).to_have_count(0)


async def open_text(page, fixtures: dict[str, Path], filename: str):
    tile = await attach(page, fixtures[filename])
    await tile.click()
    dialog = page.get_by_role("dialog", name=filename)
    await expect(dialog).to_be_visible(timeout=20_000)
    return tile, dialog


async def launch_browser(playwright, name: str):
    if name == "chrome":
        return await playwright.chromium.launch(channel="chrome", headless=True)
    if name == "msedge":
        return await playwright.chromium.launch(channel="msedge", headless=True)
    return await getattr(playwright, name).launch(headless=True)


async def exercise(base_url: str, tokens: dict, fixtures: dict[str, Path], artifact_dir: Path) -> None:
    browser_name = os.environ["STUDIO_BROWSER"]
    expect_preview = os.environ["STUDIO_EXPECT_PREVIEW"] == "1"
    case_name = os.environ["STUDIO_CASE"]
    async with async_playwright() as playwright:
        browser = await launch_browser(playwright, browser_name)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        await context.add_init_script(auth_init_script(tokens))
        page = await context.new_page()
        page_errors: list[str] = []
        console_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
        await expect(page.get_by_role("textbox", name="Message input")).to_be_visible(
            timeout=30_000
        )

        if not expect_preview:
            tile = await attach(page, fixtures["preview.txt"])
            await tile.click()
            await expect(page.get_by_role("dialog")).to_have_count(0, timeout=2_000)
            await page.screenshot(
                path=str(artifact_dir / f"{case_name}-before-no-dialog.png"),
                full_page=True,
            )
            await page.get_by_role("button", name="Remove file").click()
            pass_log(
                f"negative control {BASE_REF[:8]} reproduced: document click opens no dialog"
            )
        else:
            tile, dialog = await open_text(page, fixtures, "preview.txt")
            await expect(dialog).to_contain_text("Attachment preview fixture")
            await expect(dialog).to_contain_text("Second line stays separate.")
            await close_and_remove(page, tile)

            tile, dialog = await open_text(page, fixtures, "preview.html")
            await expect(dialog).to_contain_text("text extracted from HTML")
            await expect(dialog).to_contain_text("Fixture heading")
            await expect(dialog).not_to_contain_text("ignored()")
            await close_and_remove(page, tile)

            tile, dialog = await open_text(page, fixtures, "preview.pdf")
            await expect(dialog).to_contain_text("text extracted from PDF", timeout=30_000)
            await expect(dialog).to_contain_text("PDF first line")
            await expect(dialog).to_contain_text("PDF second line")
            await close_and_remove(page, tile)

            tile, dialog = await open_text(page, fixtures, "preview.docx")
            await expect(dialog).to_contain_text("text extracted from DOCX", timeout=30_000)
            await expect(dialog).to_contain_text("DOCX first line")
            await expect(dialog).to_contain_text("DOCX second line")
            await close_and_remove(page, tile)

            tile, dialog = await open_text(page, fixtures, "preview.odt")
            await expect(dialog).to_contain_text("text extracted from ODT", timeout=30_000)
            await expect(dialog).to_contain_text("ODT first line")
            await expect(dialog).to_contain_text("ODT second line")
            await close_and_remove(page, tile)

            tile, dialog = await open_text(page, fixtures, "long-preview.txt")
            await expect(dialog).to_contain_text("preview truncated", timeout=30_000)
            await expect(dialog).not_to_contain_text("TAIL-MUST-NOT-RENDER")
            await close_and_remove(page, tile)

            composer = page.get_by_role("textbox", name="Message input")
            await composer.fill("draft must survive a refused document")
            await page.get_by_role("button", name="Tools and attachments").click()
            async with page.expect_file_chooser() as chooser_info:
                await page.get_by_role("menuitem", name="Add photos & files").click()
            chooser = await chooser_info.value
            await chooser.set_files(str(fixtures["oversized.docx"]))
            await expect(
                page.get_by_text("DOCX XML file is too large", exact=False)
            ).to_be_visible(timeout=20_000)
            await expect(composer).to_have_value("draft must survive a refused document")
            await expect(
                page.get_by_role(
                    "button", name=re.compile(re.escape("oversized.docx"))
                )
            ).to_have_count(0)

            await page.set_viewport_size({"width": 390, "height": 844})
            tile, dialog = await open_text(page, fixtures, "preview.txt")
            box = await dialog.bounding_box()
            assert box is not None
            assert box["x"] >= 0 and box["x"] + box["width"] <= 390.5, box
            assert box["y"] >= 0 and box["y"] + box["height"] <= 844.5, box
            await page.wait_for_timeout(500)
            await page.screenshot(
                path=str(artifact_dir / f"{case_name}-after-mobile.png"),
                full_page=True,
            )
            await close_and_remove(page, tile)
            pass_log(
                f"{case_name}: TXT/HTML/PDF/DOCX/ODT/truncation/rejection/mobile"
            )

        expected_rejections = ("DOCX XML file is too large:",)
        unexpected_page_errors = [
            error
            for error in page_errors
            if not any(expected in error for expected in expected_rejections)
        ]
        relevant_console = [
            message
            for message in console_errors
            if "favicon" not in message.lower()
            and "failed to load resource" not in message.lower()
        ]
        assert not unexpected_page_errors, unexpected_page_errors
        assert not relevant_console, relevant_console
        await context.close()
        await browser.close()


def main() -> None:
    home = Path(os.environ["UNSLOTH_STUDIO_HOME"]).resolve()
    artifact_dir = Path(os.environ["STUDIO_ARTIFACT_DIR"]).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    fixtures = make_fixtures(artifact_dir / "fixtures")
    log_path = artifact_dir / "studio.log"
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc: subprocess.Popen | None = None
    try:
        proc = start_studio(home, log_path, port)
        wait_for_health(base_url)
        password = read_bootstrap_password(home, log_path)
        tokens = login_tokens(base_url, password)
        asyncio.run(exercise(base_url, tokens, fixtures, artifact_dir))
    finally:
        stop_studio(proc)


if __name__ == "__main__":
    main()
