import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

out = Path("studio/frontend/dist")
shutil.rmtree(out, ignore_errors=True)
with urllib.request.urlopen("https://pypi.org/pypi/unsloth/json", timeout=30) as response:
    metadata = json.load(response)
version = metadata["info"]["version"]
wheel = next(item for item in metadata["releases"][version] if item["packagetype"] == "bdist_wheel")
target = Path("unsloth-latest.whl")
urllib.request.urlretrieve(wheel["url"], target)
with zipfile.ZipFile(target) as archive:
    members = [name for name in archive.namelist() if name.startswith("studio/frontend/dist/")]
    if "studio/frontend/dist/index.html" not in members:
        raise SystemExit("latest PyPI wheel has no packaged frontend index")
    archive.extractall(".", members)
print(f"PyPI unsloth {version}: {sum(p.is_file() for p in out.rglob('*'))} frontend files")
target.unlink()
