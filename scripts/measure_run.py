"""보존된 Run의 실패·복구 포함 비교 자료를 읽기 전용 JSON으로 출력합니다."""

import argparse
import json
from pathlib import Path

from dev_orchestra.application.measurement import measurement
from dev_orchestra.infrastructure.store import ReadOnlyStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--version-label", required=True)
    parser.add_argument("--scenario", required=True, choices=["normal", "recovery"])
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("database must already exist")
    result = measurement(
        ReadOnlyStore(args.database),
        args.run_id,
        version_label=args.version_label,
        scenario=args.scenario,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
