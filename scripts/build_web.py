"""Build and verify local GUI assets for a CLI wheel without requiring Node at runtime."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
subprocess.run(["npm", "run", "build:web"], cwd=root, check=True)
source = root / "apps/web/dist"
manifest = {
    str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in source.rglob("*")
    if path.is_file()
}
if "index.html" not in manifest:
    raise SystemExit("GUI 빌드 결과가 없습니다")
target = root / "src/dev_orchestra/_web"
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target)
(target / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
print("GUI 정적 파일과 체크섬 manifest를 Python 패키지에 준비했습니다.")
