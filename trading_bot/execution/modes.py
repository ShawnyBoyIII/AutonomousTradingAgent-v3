from enum import Enum


class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


def require_paper_mode(mode: ExecutionMode) -> None:
    if mode is not ExecutionMode.PAPER:
        raise RuntimeError("Live execution is not available in v1")
