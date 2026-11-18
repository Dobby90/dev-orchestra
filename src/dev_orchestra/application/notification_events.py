"""Shared event facts and attention messages; channel selection stays in each service."""

MESSAGES = {
    "needs_input": (
        "입력 필요",
        "계속 진행하려면 사용자 확인 또는 입력이 필요합니다. 상세 화면을 확인해 주세요.",
    ),
    "paused": ("일시중지", "작업이 일시중지되었습니다. 계속하려면 작업을 재개해 주세요."),
    "waiting_plan_approval": (
        "기획 승인 대기",
        "기획안이 준비되었습니다. 계획을 확인하고 개발을 승인해 주세요.",
    ),
    "waiting_approval": (
        "최종 승인 대기",
        "검증된 결과가 준비되었습니다. 변경 사항과 검증 결과를 확인하고 최종 승인해 주세요.",
    ),
    "failed": ("실패", "작업 실행에 실패했습니다. 상세 화면에서 원인과 복구 방법을 확인해 주세요."),
}

# Repeated checkpoints in the same state are not new notifications.
STATE_TRANSITIONS = """
WITH transitions AS (
 SELECT id,run_id,timestamp,json_extract(data,'$.state') AS state,
 lag(json_extract(data,'$.state')) OVER (PARTITION BY run_id ORDER BY id) AS previous
 FROM events WHERE kind IN ('state','state.changed')
)
"""
