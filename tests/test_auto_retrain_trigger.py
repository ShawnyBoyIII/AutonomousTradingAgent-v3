from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import auto_retrain_trigger


def test_trigger_retrain_dry_run_does_not_spawn_process(monkeypatch) -> None:
    def fail_run(*args, **kwargs):
        raise AssertionError("dry run should not spawn a subprocess")

    monkeypatch.setattr("subprocess.run", fail_run)

    result = auto_retrain_trigger.trigger_retrain(
        ["AAPL"],
        dry_run=True,
        epochs=5,
        timesteps=100,
    )

    assert result == {
        "status": "dry_run",
        "symbols": ["AAPL"],
        "epochs": 5,
        "timesteps": 100,
    }


def test_trigger_retrain_resolves_daily_supermodel_script_from_repo(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd, capture_output=False):
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        return SimpleNamespace(returncode=0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = auto_retrain_trigger.trigger_retrain(
        ["AAPL", "MSFT"],
        dry_run=False,
        epochs=10,
        timesteps=1000,
        output_dir="state/rl_logs/test",
    )

    cmd = captured["cmd"]
    assert Path(cmd[1]).is_absolute()
    assert Path(cmd[1]).name == "daily_supermodel.py"
    assert Path(cmd[1]).parent.name == "scripts"
    assert "--symbols" in cmd
    assert "AAPL,MSFT" in cmd
    assert result["status"] == "trained"
    assert result["exit_code"] == 0
