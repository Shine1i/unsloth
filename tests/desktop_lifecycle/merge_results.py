# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Merge per-job lifecycle evidence and enforce scenario disposition coverage."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
STATUS_ORDER = {
    "verified": 0,
    "blocked": 1,
    "not reproducible": 2,
    "failed": 3,
}


def audit_scenarios(path: Path) -> dict[str, str]:
    scenarios: dict[str, str] = {}
    row = re.compile(
        r"^\|\s*([A-Z]+-\d{2})\s*\|.*\|\s*(\*\*)?(P[012])(?:\*\*)?\s*\|\s*$"
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        match = row.match(line)
        if match:
            scenarios[match.group(1)] = match.group(3)
    if len(scenarios) != 60:
        raise SystemExit(f"Expected 60 audit scenarios, found {len(scenarios)}")
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-priority", choices=("P0", "P1", "P2"))
    args = parser.parse_args()

    scenarios = audit_scenarios(args.audit)
    merged: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    for path in sorted(args.input.rglob("results.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        sources.append(str(path))
        for result in payload.get("results", []):
            scenario = result["scenario"]
            if scenario not in scenarios:
                raise SystemExit(f"{path}: unknown scenario {scenario}")
            if scenario in merged:
                previous = merged[scenario]
                # A concrete live result supersedes a platform-specific block.
                if previous["status"] == "blocked" and result["status"] != "blocked":
                    merged[scenario] = result
                elif result["status"] == "blocked" and previous["status"] != "blocked":
                    continue
                else:
                    # Some catalog rows deliberately contain multiple required
                    # package/platform subcases (PKG-03 is deb + AppImage). Keep
                    # every live observation and give the combined row the most
                    # severe disposition: one failed required subcase fails the
                    # row; all verified subcases verify it.
                    prior_observations = previous.pop(
                        "observations", [dict(previous)]
                    )
                    new_observations = result.pop("observations", [dict(result)])
                    observations = prior_observations + new_observations
                    worst = max(
                        observations,
                        key=lambda item: STATUS_ORDER[item["status"]],
                    )["status"]
                    merged[scenario] = {
                        "scenario": scenario,
                        "status": worst,
                        "summary": " | ".join(
                            f"{item['platform']}: {item['summary']}"
                            for item in observations
                        ),
                        "started_at": min(item["started_at"] for item in observations),
                        "completed_at": max(item["completed_at"] for item in observations),
                        "platform": "multiple",
                        "commands": [
                            command
                            for item in observations
                            for command in item.get("commands", [])
                        ],
                        "evidence": [
                            artifact
                            for item in observations
                            for artifact in item.get("evidence", [])
                        ],
                        "mismatch": " | ".join(
                            item["mismatch"]
                            for item in observations
                            if item.get("mismatch")
                        )
                        or None,
                        "limitation": " | ".join(
                            item["limitation"]
                            for item in observations
                            if item.get("limitation")
                        )
                        or None,
                        "observations": observations,
                    }
            else:
                merged[scenario] = result

    if args.require_priority:
        maximum = PRIORITY_ORDER[args.require_priority]
        required = {
            scenario
            for scenario, priority in scenarios.items()
            if PRIORITY_ORDER[priority] <= maximum
        }
        missing = sorted(required - merged.keys())
        if missing:
            raise SystemExit(
                f"Missing live disposition for {args.require_priority}-or-higher scenarios: "
                + ", ".join(missing)
            )

    output = {
        "schema": 1,
        "audit": str(args.audit),
        "sources": sources,
        "scenario_priorities": scenarios,
        "results": [merged[key] for key in sorted(merged)],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "result_count": len(merged),
                "verified": sum(item["status"] == "verified" for item in merged.values()),
                "failed": sum(item["status"] == "failed" for item in merged.values()),
                "not_reproducible": sum(
                    item["status"] == "not reproducible" for item in merged.values()
                ),
                "blocked": sum(item["status"] == "blocked" for item in merged.values()),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
