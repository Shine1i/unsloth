#!/usr/bin/env python3
"""Exercise start._launch through a real npm-generated Windows command shim."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from unsloth_cli.commands import start


PACKAGE_NAME = "pr7567-capture-agent"
PROMPT = (
    "Step 1: add 2 + 2.\r\n"
    "Step 2: explain \"division by zero\".\r\n"
    "Step 3: emit sentinel PR7567_END."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-label", required = True)
    parser.add_argument(
        "--target-style",
        choices = ("js", "extensionless", "shebang-args"),
        required = True,
    )
    parser.add_argument("--expected-npm", required = True)
    args = parser.parse_args()

    repo = Path.cwd()
    repro_root = repo / ".repro"
    shutil.rmtree(repro_root, ignore_errors = True)
    package_dir = repro_root / "package"
    package_dir.mkdir(parents = True)

    target_name = "capture" if args.target_style == "extensionless" else "capture.js"
    shebang = (
        "#!/usr/bin/env node --no-warnings"
        if args.target_style == "shebang-args"
        else "#!/usr/bin/env node"
    )
    target = package_dir / target_name
    target.write_text(
        shebang
        + "\n"
        + "const fs = require('fs');\n"
        + "fs.writeFileSync(process.env.CAPTURE_PATH, "
        + "JSON.stringify(process.argv.slice(2)), 'utf8');\n",
        encoding = "utf-8",
    )
    (package_dir / "package.json").write_text(
        json.dumps(
            {
                "name": PACKAGE_NAME,
                "version": "1.0.0",
                "private": True,
                "bin": {"pr7567-capture": target_name},
            },
            indent = 2,
        )
        + "\n",
        encoding = "utf-8",
    )

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is not on PATH")
    npm_version = subprocess.check_output([npm, "--version"], text = True).strip()
    print(f"NPM_VERSION={npm_version}")
    if npm_version != args.expected_npm:
        raise SystemExit(f"expected npm {args.expected_npm}, got {npm_version}")

    subprocess.run(
        [npm, "uninstall", "--global", PACKAGE_NAME],
        check = False,
        stdout = subprocess.DEVNULL,
        stderr = subprocess.DEVNULL,
    )
    pack_output = subprocess.check_output(
        [
            npm,
            "pack",
            str(package_dir),
            "--pack-destination",
            str(repro_root),
        ],
        text = True,
    )
    tarball = repro_root / pack_output.strip().splitlines()[-1]
    print(f"PACKAGE_TARBALL={tarball}")
    subprocess.run([npm, "install", "--global", str(tarball)], check = True)

    executable = shutil.which("pr7567-capture")
    if executable is None:
        raise SystemExit("npm-installed pr7567-capture shim is not on PATH")
    shim = Path(executable)
    shim_text = shim.read_text(encoding = "utf-8")
    (repro_root / "generated-shim.cmd.txt").write_text(shim_text, encoding = "utf-8")
    print(f"SHIM_PATH={shim}")
    print("SHIM_BEGIN")
    print(shim_text.rstrip())
    print("SHIM_END")

    predicted = start._resolved_launch_command(str(shim), [PROMPT])
    print(f"RESOLVED_EXECUTABLE={predicted[0]}")
    print(f"BYPASS_ACTIVE={Path(predicted[0]).suffix.lower() not in {'.cmd', '.bat'}}")

    capture_path = repro_root / "captured-argv.json"
    os.environ["CAPTURE_PATH"] = str(capture_path)
    returncode = start._launch(
        ["pr7567-capture", PROMPT],
        {},
        install_hint = "unused",
    )
    actual = None
    if capture_path.exists():
        actual = json.loads(capture_path.read_text(encoding = "utf-8"))

    result = {
        "case": args.case_label,
        "npm_version": npm_version,
        "target_style": args.target_style,
        "shim_path": str(shim),
        "resolved_command": predicted,
        "returncode": returncode,
        "expected_argv": [PROMPT],
        "actual_argv": actual,
    }
    (repro_root / "result.json").write_text(
        json.dumps(result, indent = 2) + "\n",
        encoding = "utf-8",
    )
    print("RESULT=" + json.dumps(result, ensure_ascii = True))

    failures = []
    if Path(predicted[0]).suffix.lower() in {".cmd", ".bat"}:
        failures.append("npm shim was not bypassed")
    if returncode != 0:
        failures.append(f"launch exited {returncode}")
    if actual != [PROMPT]:
        failures.append(f"captured argv was {actual!r}")
    if failures:
        print(f"FAIL case={args.case_label}: " + "; ".join(failures))
        return 1

    print(f"PASS case={args.case_label}: multiline and quoted prompt preserved exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
