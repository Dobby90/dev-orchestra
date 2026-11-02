from typer.testing import CliRunner

from dev_orchestra.cli import app


def test_cli_help():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ["run", "status", "list", "logs", "resume", "cancel", "report", "approve"]:
        assert name in result.output


def test_cli_queries(tmp_path):
    cli = CliRunner()
    opts = ["--database", str(tmp_path / "db.sqlite")]
    assert cli.invoke(app, opts + ["list"]).exit_code == 0
    result = cli.invoke(app, opts + ["status", "unknown"])
    assert result.exit_code == 1
    assert "unknown run" in result.output
    assert cli.invoke(app, opts + ["cancel", "unknown"]).exit_code == 1
