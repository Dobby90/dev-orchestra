"""Export public contracts using an isolated empty DB, never a user's runtime."""

import argparse
import json
import tempfile
from pathlib import Path

from dev_orchestra.api.main import create_app
from dev_orchestra.config import Config, Storage


def schema():
    with tempfile.TemporaryDirectory(prefix="devorchestra-schema-") as directory:
        root = Path(directory)
        config = Config(
            storage=Storage(
                database=root / "state.db",
                runs_directory=root / "runs",
                worktrees_directory=root / "worktrees",
            )
        )
        return json.dumps(create_app(config, []).openapi(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("apps/web/openapi.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = schema()
    if args.check:
        if args.output.read_text() != content:
            raise SystemExit("공개 API 스키마가 변경되었습니다. export_web_schema.py를 실행하세요.")
    else:
        args.output.write_text(content)
