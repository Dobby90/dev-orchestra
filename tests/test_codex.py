import json
import sys

import pytest

from dev_orchestra.config import Codex
from dev_orchestra.domain.models import AgentRequest, AgentResult, PipelineError
from dev_orchestra.infrastructure.codex import CodexClient, prune_unused_definitions, strict_schema


def test_strict_model_schema_has_no_open_dicts():
    schema = strict_schema(AgentResult.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["coverage"]["type"] == "array"


def test_role_schema_keeps_nested_and_recursive_refs():
    def ref(name):
        return {"$ref": "#/$defs/" + name}

    schema = {
        "properties": {"result": {"anyOf": [ref("Row"), {"type": "null"}]}},
        "$defs": {"Row": {"items": ref("Item")}, "Item": {"items": ref("Row")}, "Unused": {}},
    }
    prune_unused_definitions(schema)
    assert set(schema["$defs"]) == {"Row", "Item"}
    assert schema["$defs"]["Row"]["items"] == ref("Item")


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
    assert 'features.apps=false' in sys.argv and 'features.plugins=false' in sys.argv
    assert 'web_search="disabled"' in sys.argv
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
    schema = json.loads((tmp_path / "attempt" / "schema.json").read_text())
    assert set(schema["$defs"]) == {"Issue"}


@pytest.mark.parametrize("contract2", [True, False])
def test_review_v2_rejects_legacy_shape_for_new_contract(tmp_path, contract2):
    from dev_orchestra.domain.models import ReviewResultV2

    stub = tmp_path / "review-stub"
    response = (
        ReviewResultV2(summary="review") if contract2 else AgentResult(summary="legacy")
    ).model_dump_json()
    stub.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
sys.stdin.read()
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text({response!r})
""")
    stub.chmod(0o700)
    client = CodexClient(Codex(command=str(stub)))
    request = AgentRequest(
        run_id="r",
        role="reviewer",
        worktree=str(tmp_path),
        iteration=1,
        input_sha="a" * 40,
        context={"review_contract": 2},
    )
    if contract2:
        assert isinstance(client.run(request, tmp_path / "result"), ReviewResultV2)
    else:
        with pytest.raises(PipelineError, match="schema"):
            client.run(request, tmp_path / "result")
    schema = json.loads((tmp_path / "result" / "schema.json").read_text())
    assert set(schema["$defs"]) == {"Issue", "ReviewAssessment", "ReviewIssueDistinction"}
