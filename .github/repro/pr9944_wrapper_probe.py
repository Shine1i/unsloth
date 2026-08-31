# Unsloth Zoo - Utilities for Unsloth
# Copyright 2023-present Daniel Han-Chen, Michael Han-Chen & the Unsloth team. All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Cross-platform A/B probe for the trainer-kwarg routing change in PR #9944."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import sys
import tempfile
from functools import wraps
from pathlib import Path

import torch
import trl
from packaging.version import Version as PackagingVersion
from transformers import TrainingArguments
from trl.trainer.sft_config import SFTConfig


def Version(value):
    return PackagingVersion(getattr(value, "__version__", value))


def load_wrapper(source_path: Path):
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_ensure_warnings_issued",
        "_resolve_trainer_params",
        "_backwards_compatible_trainer",
    }
    wanted = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {
        "torch": torch,
        "trl": trl,
        "Version": Version,
        "inspect": inspect,
        "dataclasses": dataclasses,
        "wraps": wraps,
    }
    exec(compile(ast.Module(wanted, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["_backwards_compatible_trainer"]


class StrictTrainer:
    def __init__(
        self,
        model=None,
        args=None,
        train_dataset=None,
        processing_class=None,
    ):
        self.args = args
        self.train_dataset = train_dataset
        self.processing_class = processing_class


class VariadicTrainer:
    def __init__(self, model=None, args=None, train_dataset=None, **kwargs):
        self.args = args
        self.train_dataset = train_dataset
        self.extra = kwargs


@dataclasses.dataclass
class PlainConfig:
    output_dir: str = "out"
    packing: bool = False


def config_kwargs():
    parameters = inspect.signature(SFTConfig).parameters
    values = {"output_dir": tempfile.mkdtemp(), "report_to": []}
    if "use_cpu" in parameters:
        values["use_cpu"] = True
    if "bf16" in parameters:
        values["bf16"] = False
    if "fp16" in parameters:
        values["fp16"] = False
    return values


def outcome(call):
    try:
        value = call()
        return {"kind": "return", "value": value}
    except Exception as exc:
        return {"kind": "raise", "type": type(exc).__name__, "message": str(exc)}


def run_side(root: Path):
    wrap = load_wrapper(root / "unsloth" / "trainer.py")

    class Strict(StrictTrainer):
        pass

    class Variadic(VariadicTrainer):
        pass

    Strict.__init__ = wrap(Strict, SFTConfig)
    Variadic.__init__ = wrap(Variadic, SFTConfig)

    known = Strict(
        model=None,
        args=SFTConfig(**config_kwargs()),
        train_dataset="dataset",
        packing=True,
        max_length=2048,
        tokenizer="tokenizer",
    )
    unknown_strict = outcome(
        lambda: Strict(model=None, args=SFTConfig(**config_kwargs()), bogus_kwarg=7)
    )
    unknown_none = outcome(
        lambda: Strict(model=None, args=SFTConfig(**config_kwargs()), bogus_kwarg=None)
    )
    unknown_variadic = outcome(
        lambda: Variadic(model=None, args=SFTConfig(**config_kwargs()), bogus_kwarg=7)
    )
    if unknown_variadic["kind"] == "return":
        unknown_variadic["value"] = unknown_variadic["value"].extra

    legacy = outcome(
        lambda: Strict(model=None, args=SFTConfig(**config_kwargs()), max_seq_length=2048)
    )
    if legacy["kind"] == "return":
        legacy["value"] = {
            "max_seq_length": getattr(legacy["value"].args, "max_seq_length", "<absent>"),
            "max_length": getattr(legacy["value"].args, "max_length", "<absent>"),
        }

    @dataclasses.dataclass
    class GeneratedLikeSFTConfig(SFTConfig):
        max_seq_length: int | None = None

    class GeneratedClosureTrainer(StrictTrainer):
        pass

    GeneratedClosureTrainer.__init__ = wrap(GeneratedClosureTrainer, GeneratedLikeSFTConfig)
    pristine_config = SFTConfig(**config_kwargs())
    generated_closure = outcome(
        lambda: GeneratedClosureTrainer(
            model=None,
            args=pristine_config,
            max_seq_length=2048,
        )
    )
    if generated_closure["kind"] == "return":
        generated_closure["value"] = {
            "same_object": generated_closure["value"].args is pristine_config,
            "max_seq_length": getattr(
                generated_closure["value"].args, "max_seq_length", "<absent>"
            ),
            "max_length": generated_closure["value"].args.max_length,
        }

    no_args = outcome(lambda: Strict(model=None, bogus_kwarg=7))

    class PlainTrainer(StrictTrainer):
        pass

    PlainTrainer.__init__ = wrap(PlainTrainer, PlainConfig)
    plain_config = outcome(
        lambda: PlainTrainer(model=None, args=PlainConfig(), bogus_kwarg=7)
    )

    base_training_args = TrainingArguments(
        output_dir=tempfile.mkdtemp(), use_cpu=True, report_to=[]
    )
    moved = Strict(
        model=None, args=base_training_args, packing=True, max_length=333
    )

    return {
        "config_has_max_seq_length": "max_seq_length"
        in {field.name for field in dataclasses.fields(SFTConfig)},
        "known": {
            "packing": known.args.packing,
            "max_length": known.args.max_length,
            "train_dataset": known.train_dataset,
            "processing_class": known.processing_class,
        },
        "unknown_strict": unknown_strict,
        "unknown_none": unknown_none,
        "unknown_variadic": unknown_variadic,
        "legacy_max_seq_length": legacy,
        "generated_closure_pristine_args": generated_closure,
        "no_args_unknown": no_args,
        "plain_dataclass_unknown": plain_config,
        "base_training_args_move": {
            "same_object": moved.args is base_training_args,
            "packing": getattr(moved.args, "packing", "<absent>"),
            "max_length": getattr(moved.args, "max_length", "<absent>"),
        },
    }


def assert_expected(result):
    base = result["base"]
    head = result["head"]

    assert base["known"] == head["known"] == {
        "packing": True,
        "max_length": 2048,
        "train_dataset": "dataset",
        "processing_class": "tokenizer",
    }
    assert base["unknown_strict"]["kind"] == "return"
    assert head["unknown_strict"]["kind"] == "raise"
    assert head["unknown_strict"]["type"] == "TypeError"
    assert "bogus_kwarg" in head["unknown_strict"]["message"]
    assert base["unknown_none"]["kind"] == "return"
    assert head["unknown_none"]["kind"] == "raise"
    assert base["unknown_variadic"]["value"] == {}
    assert head["unknown_variadic"]["value"] == {"bogus_kwarg": 7}
    assert base["generated_closure_pristine_args"] == head[
        "generated_closure_pristine_args"
    ] == {
        "kind": "return",
        "value": {
            "same_object": True,
            "max_seq_length": 2048,
            "max_length": 1024,
        },
    }
    assert base["base_training_args_move"] == head["base_training_args_move"] == {
        "same_object": True,
        "packing": True,
        "max_length": 333,
    }
    assert base["no_args_unknown"]["kind"] == "raise"
    assert head["no_args_unknown"]["kind"] == "raise"
    assert base["plain_dataclass_unknown"]["kind"] == "raise"
    assert head["plain_dataclass_unknown"]["kind"] == "raise"

    if head["config_has_max_seq_length"]:
        assert base["legacy_max_seq_length"] == head["legacy_max_seq_length"]
        assert head["legacy_max_seq_length"]["kind"] == "return"
    else:
        assert base["legacy_max_seq_length"]["kind"] == "return"
        assert head["legacy_max_seq_length"]["kind"] == "raise"
        assert "max_seq_length" in head["legacy_max_seq_length"]["message"]


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: pr9944_wrapper_probe.py BASE_ROOT HEAD_ROOT")
    result = {
        "python": sys.version,
        "platform": sys.platform,
        "trl": trl.__version__,
        "base": run_side(Path(sys.argv[1])),
        "head": run_side(Path(sys.argv[2])),
    }
    assert_expected(result)
    print(json.dumps(result, sort_keys=True, default=str))
    print("PASS: PR #9944 fails loudly for unknown kwargs and preserves valid paths")


if __name__ == "__main__":
    main()
