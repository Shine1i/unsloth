import json
import os
from pathlib import Path

log = Path(os.environ["BENCH_LOG"])
markers = {}
for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
    marker = line.strip()
    if "UNSLOTH_BENCHMARK " in marker:
        marker = marker[marker.index("UNSLOTH_BENCHMARK "):]
        parts = marker.split()
        if len(parts) >= 3:
            markers[parts[1]] = int(parts[2])

def duration(name):
    start, end = markers.get(f"{name}_start"), markers.get(f"{name}_end")
    return 0 if start is None or end is None else end - start

scenario = os.environ["SCENARIO"]
data = {
    "os": os.environ["OS_LABEL"],
    "scenario": scenario,
    "repetition": int(os.environ["REPETITION"]),
    "total_ms": int(os.environ["TOTAL_MS"]),
    "node_install_ms": duration("node_install"),
    "frontend_deps_ms": duration("frontend_deps"),
    "frontend_build_ms": duration("frontend_build"),
    "oxc_install_ms": duration("oxc_install"),
    "markers": markers,
    "actual_installer": True,
}
required = {
    "full_build": ["node_install", "frontend_deps", "frontend_build", "oxc_install"],
    "packaged_skip": ["node_install", "oxc_install"],
    "no_node_oxc": [],
}[scenario]
for phase in required:
    if data[f"{phase}_ms"] <= 0:
        raise SystemExit(f"missing {phase} timing: {data}")
for phase in {
    "packaged_skip": ["frontend_deps", "frontend_build"],
    "no_node_oxc": ["node_install", "frontend_deps", "frontend_build", "oxc_install"],
}.get(scenario, []):
    if data[f"{phase}_ms"]:
        raise SystemExit(f"unexpected {phase}: {data}")
Path(os.environ["BENCH_METRICS"]).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(json.dumps(data, indent=2))
