import json
import sys

import pytest

from dev_orchestra.config import Codex
from dev_orchestra.domain.models import AgentRequest, AgentResult, PipelineError
from dev_orchestra.infrastructure.codex import CodexClient, strict_schema


def test_strict_model_schema_has_no_open_dicts():
    schema = strict_schema(AgentResult.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["coverage"]["type"] == "array"


@pytest.mark.parametrize("valid", [True, False])
def test_codex_process_contract(tmp_path, valid):
    stub = tmp_path / "codex-stub"
    response = json.dumps(AgentResult(summary="ok").model_dump()) if valid else "invalid"
    stub.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
if '--help' in sys.argv:
    print('--output-schema --ephemeral --ignore-user-config --json')
else:
    assert '--sandbox' in sys.argv and 'read-only' in sys.argv
    assert '--ephemeral' in sys.argv and 'resume' not in sys.argv
    assert 'UNTRUSTED TASK DATA' in sys.stdin.read()
    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text({response!r})
    print('{{"type":"turn.completed"}}')
""")
    stub.chmod(0o755)
    client = CodexClient(Codex(command=str(stub)))
    client.check()
    req = AgentRequest(
        run_id="r",
        role="reviewer",
        worktree=str(tmp_path),
        iteration=0,
        input_sha="abc",
        context={},
    )
    if valid:
        assert client.run(req, tmp_path / "attempt").summary == "ok"
    else:
        with pytest.raises(PipelineError, match="schema"):
            client.run(req, tmp_path / "attempt")
