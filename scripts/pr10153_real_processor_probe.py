# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""A/B probe for PR 10153 using a real public Transformers vision processor."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
from pathlib import Path
import sys
import types

import transformers
from transformers import AutoProcessor

MODEL_ID = "HuggingFaceTB/SmolVLM-256M-Instruct"


def load_get_chat_template(source: Path):
    unsloth_dir = source / "unsloth"
    package = types.ModuleType("unsloth")
    package.__path__ = [str(unsloth_dir)]
    sys.modules["unsloth"] = package

    module_path = unsloth_dir / "chat_templates.py"
    spec = importlib.util.spec_from_file_location("unsloth.chat_templates", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["unsloth.chat_templates"] = module
    spec.loader.exec_module(module)

    tokenizer_utils = types.ModuleType("unsloth_zoo.tokenizer_utils")
    tokenizer_utils.patch_tokenizer = lambda model, tokenizer: (model, tokenizer)
    sys.modules["unsloth_zoo.tokenizer_utils"] = tokenizer_utils
    return module.get_chat_template


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--expect", choices=("attribute-error", "pass"), required=True
    )
    args = parser.parse_args()

    source = args.source.resolve()
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    original_side = processor.tokenizer.padding_side
    source_sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"SOURCE_SHA={source_sha}")
    print(f"TRANSFORMERS_VERSION={transformers.__version__}")
    print(f"PROCESSOR_CLASS={type(processor).__name__}")
    print(f"PROCESSOR_HAS_PADDING_SIDE={hasattr(processor, 'padding_side')}")
    print(f"TOKENIZER_PADDING_SIDE={original_side}")

    get_chat_template = load_get_chat_template(source)
    try:
        result = get_chat_template(
            processor,
            chat_template=("{{ messages }}", "<end_of_utterance>"),
            patch_saving=False,
            use_zoo_tokenizer_patch=True,
        )
    except AttributeError as error:
        if args.expect != "attribute-error" or "padding_side" not in str(error):
            raise
        print(f"EXPECTED_BASE_ERROR={type(error).__name__}: {error}")
        print("PR10153_NEGATIVE_BASE_PASS")
        return

    if args.expect == "attribute-error":
        raise AssertionError("base unexpectedly accepted a processor without padding_side")
    assert result is processor
    assert result.tokenizer.padding_side == original_side
    assert result.padding_side == original_side
    print(f"PROCESSOR_PADDING_SIDE_AFTER={result.padding_side}")
    print(f"TOKENIZER_PADDING_SIDE_AFTER={result.tokenizer.padding_side}")
    print("PR10153_POSITIVE_HEAD_PASS")


if __name__ == "__main__":
    main()
