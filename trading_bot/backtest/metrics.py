def compute_win_rate(wins: int, losses: int) -> float:
    total = wins + losses
    return 0.0 if total == 0 else wins / total
