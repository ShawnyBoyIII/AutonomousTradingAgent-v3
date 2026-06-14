import pytest

from trading_bot.execution.modes import ExecutionMode, require_paper_mode


def test_require_paper_mode_rejects_live() -> None:
    with pytest.raises(RuntimeError):
        require_paper_mode(ExecutionMode.LIVE)
