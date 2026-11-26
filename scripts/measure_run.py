"""보존된 Run의 실패·복구 포함 비교 자료를 JSON 또는 역할별 표로 출력합니다."""

import argparse
import json
from pathlib import Path

from dev_orchestra.application.measurement import measurement
from dev_orchestra.infrastructure.store import ReadOnlyStore


def role_table(result):
    headers = (
        "Role",
        "Attempts",
        "Stage s",
        "CLI s",
        "Input",
        "Cached",
        "Uncached",
        "Output",
        "Total",
        "Usage",
    )
    rows = []
    for role in result["role_summary"]:
        usage = role["usage"] or {}
        counts = (
            usage.get("input_tokens"),
            usage.get("cached_input_tokens"),
            role["uncached_input_tokens"],
            usage.get("output_tokens"),
            role["total_tokens"],
        )
        rows.append(
            (
                role["role"],
                str(role["total_attempts"]),
                *(
                    f"{seconds:.2f}" if seconds is not None else "—"
                    for seconds in (role["wall_seconds"], role["cli_seconds"])
                ),
                *(f"{count:,}" if count is not None else "—" for count in counts),
                role["usage_status"],
            )
        )
    widths = [max(map(len, column)) for column in zip(headers, *rows, strict=True)]
    return "\n".join(
        "  ".join(cell.rjust(width) for cell, width in zip(row, widths, strict=True))
        for row in [headers, *rows]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--version-label", required=True)
    parser.add_argument("--scenario", required=True, choices=["normal", "recovery"])
    parser.add_argument("--format", choices=["json", "table"], default="json")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("database must already exist")
    result = measurement(
        ReadOnlyStore(args.database),
        args.run_id,
        version_label=args.version_label,
        scenario=args.scenario,
    )
    if args.format == "table":
        print(role_table(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
