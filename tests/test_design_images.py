"""Design references across uploads, Git worktrees, approval and offline Codex calls."""

import os
import struct
import sys
import time
import zlib
from pathlib import Path

import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_workflow import FakeAgent, FakeTests, WorkerRunner
from test_workflow import service as service
from typer.testing import CliRunner

from dev_orchestra.application.backend import IMAGE_UPLOAD_TTL_SECONDS
from dev_orchestra.application.plan_approval import binding, decide
from dev_orchestra.application.service import RunService
from dev_orchestra.cli import app
from dev_orchestra.config import Codex
from dev_orchestra.domain.models import AgentRequest, AgentResult, PipelineError
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.design_images import verified_run_images
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.git import Git


def png(red=0):
    def chunk(name, content):
        body = name + content
        return struct.pack(">I", len(content)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0])))
        + chunk(b"IEND", b"")
    )


def upload(client, name, content):
    response = client.post("/api/design-images", files={"file": (name, content, "image/png")})
    assert response.status_code == 201, response.text
    return response.json()


def proposal_for(client, service, repo, images):
    registered = project(client, service, repo)
    spec = client.post(
        "/api/specs",
        files={"file": ("task.md", b"See [step](assets/before-1.png)", "text/markdown")},
    )
    assert spec.status_code == 201
    return {
        "name": "디자인 시뮬레이션",
        "project_id": registered["id"],
        "spec": {"kind": "stored", "spec_id": spec.json()["id"]},
        "design_images": images,
    }


def preview(client, proposal):
    return client.post(f"/api/projects/{proposal['project_id']}/run-previews", json=proposal)


def test_four_uploaded_images_reach_visual_roles_and_survive_staging_cleanup(web, monkeypatch):
    client, service, _, repo, _ = web
    monkeypatch.undo()
    names = ["before-1.png", "before-2.png", "after-1.png", "after-2.png"]
    files = [png(i) for i in range(len(names))]
    images = []
    for name, data in zip(names, files, strict=True):
        local = repo / name
        local.write_bytes(data)
        images.append(upload(client, name, local.read_bytes()))
    proposal = proposal_for(client, service, repo, images)
    receipt = preview(client, proposal)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["design_images"] == images
    body = {**proposal, "preview_token": receipt.json()["preview_token"]}
    headers = {"Idempotency-Key": "submit-design"}
    assert (
        client.post(
            "/api/runs", json={**body, "design_images": images[::-1]}, headers=headers
        ).status_code
        == 409
    )
    accepted = client.post("/api/runs", json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    rid = accepted.json()["run_id"]
    run = service.store.get(rid)
    assert client.get(f"/api/runs/{rid}/spec").json()["design_images"] == run["design_images"]
    for image, data in zip(images, files, strict=True):
        staged = f"/api/design-images/{image['id']}"
        assert client.get(staged).content == data
        client.app.state.backend.design_image_upload_path(image["id"]).unlink()
        assert client.get(staged).status_code == 404
        result = client.get(f"/api/runs/{rid}/design-images/{image['sha256']}")
        assert result.headers["content-type"] == "image/png" and result.content == data
    assert client.post("/api/runs", json=body, headers=headers).json() == accepted.json()

    class DesignAgent(FakeAgent):
        def run(self, request, directory, stop):
            if request.role == "tester":
                assert not request.design_image_paths
            else:
                assert request.context["design_images"] == names
                assert [path.read_bytes() for path in request.design_image_paths] == files
            assert not any((Path(request.worktree) / name).exists() for name in names)
            return super().run(request, directory, stop)

    agent = DesignAgent(block=1)
    runner = WorkerRunner(service.store, agent, FakeTests())
    runner.execute(rid)
    run = service.store.get(rid)
    assert run["state"] == "waiting_plan_approval", run.get("error")
    approved_hash = binding(service.store, run)["plan_hash"]
    first = verified_run_images(run)[0]
    first.write_bytes(png(99))
    with pytest.raises(PipelineError, match=r"changed design reference \[before-1.png\]"):
        decide(service, rid, approved_hash, True)
    first.write_bytes(files[0])
    decide(service, rid, approved_hash, True)
    runner.execute(rid)
    run = service.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == [
        "planner",
        "developer",
        "tester",
        "reviewer",
        "fixer",
        "tester",
        "reviewer",
    ]
    service.approve(rid)
    assert all((repo / name).read_bytes() == data for name, data in zip(names, files, strict=True))
    assert set(Git(repo).call("ls-tree", "--name-only", run["final_sha"]).splitlines()) == {
        "main.py"
    }


def test_upload_validation_expiration_and_authentication(web):
    client, service, _, repo, _ = web
    for name, content in [
        ("fake.png", b"<svg>"),
        ("broken.png", png()[:-1]),
        ("../escape.png", png()),
        ("image.jpg", png()),
    ]:
        response = client.post("/api/design-images", files={"file": (name, content, "image/png")})
        assert response.status_code == 422
        assert response.json()["code"] == "DESIGN_IMAGE_INVALID"
    image = upload(client, "design.png", png())
    path = f"/api/design-images/{image['id']}"
    assert (
        client.post(
            "/api/design-images",
            files={"file": ("image.png", png(), "image/png")},
            headers={"X-CSRF-Token": "incorrect"},
        ).status_code
        == 403
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(client, "_cookies", type(client.cookies)())
        assert client.get(path).status_code == 401
    proposal = proposal_for(client, service, repo, [image])
    duplicate = preview(client, {**proposal, "design_images": [image, image]})
    assert duplicate.status_code == 422
    assert duplicate.json()["code"] == "DESIGN_IMAGE_DUPLICATE"
    staged = client.app.state.backend.design_image_upload_path(image["id"])
    staged.write_bytes(png(9))
    assert preview(client, proposal).json()["code"] == "DESIGN_IMAGE_INVALID"
    expired = time.time() - IMAGE_UPLOAD_TTL_SECONDS - 1
    os.utime(staged, (expired, expired))
    assert preview(client, proposal).status_code == 404
    upload(client, "replacement.png", png(1))
    assert not staged.exists()


def test_cli_repeatable_image_option_snapshots_local_files(service, monkeypatch, tmp_path):
    svc, old, repo = service
    config = tmp_path / "config.yaml"
    write(config, svc.config.model_dump(mode="json"))
    spec = Path(svc.store.get(old)["directory"]) / "spec.md"
    paths = [repo / "screen 1.png", repo / "screen 2.png"]
    for number, path in enumerate(paths):
        path.write_bytes(png(number))
    monkeypatch.setattr(RunService, "start", lambda *_args, **_kwargs: None)
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "run",
            "--repo",
            str(repo),
            "--spec",
            str(spec),
            "--image",
            str(paths[0]),
            "--image",
            str(paths[1]),
            "--detach",
        ],
    )
    assert result.exit_code == 0, result.output
    run = svc.store.get(result.output.strip())
    for path in paths:
        path.unlink()
    assert [image["name"] for image in run["design_images"]] == [p.name for p in paths]
    assert [path.read_bytes() for path in verified_run_images(run)] == [png(), png(1)]


@pytest.mark.parametrize("session", [None, "11111111-1111-4111-8111-111111111111"])
def test_codex_passes_four_images_and_filename_context_without_host_paths(tmp_path, session):
    paths = [tmp_path / f"screen {i}.png" for i in range(4)]
    for i, path in enumerate(paths):
        path.write_bytes(png(i))
    stub = tmp_path / "codex-stub"
    response = AgentResult(summary="review").model_dump_json()
    stub.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
args = sys.argv[1:]
images = [args[i + 1] for i, arg in enumerate(args) if arg == '--image']
assert images == {[str(path) for path in paths]!r}
assert args[-1] == '-'
if {session!r}:
    assert args[-2] == {session!r} and 'resume' in args
prompt = sys.stdin.read()
assert 'numbered callouts' in prompt and 'design_images' in prompt
for path in images:
    assert path not in prompt and Path(path).name in prompt and Path(path).is_file()
Path(args[args.index('--output-last-message') + 1]).write_text({response!r})
""")
    stub.chmod(0o700)
    client = CodexClient(Codex(command=str(stub)))
    request = AgentRequest(
        run_id="run",
        role="reviewer",
        worktree=str(tmp_path / "worktree"),
        iteration=0,
        input_sha="abc",
        context={"design_images": [path.name for path in paths]},
        design_image_paths=tuple(paths),
    )
    Path(request.worktree).mkdir()
    assert client.run(request, tmp_path / "stage", _session_id=session).summary == "review"
