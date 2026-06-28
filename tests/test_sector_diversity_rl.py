from scripts.sector_diversity_rl import _confidence_verdict, _model_path, _parse_seeds


def test_parse_seeds() -> None:
    assert _parse_seeds("42, 123,789") == [42, 123, 789]


def test_model_path_uses_seed_name() -> None:
    assert _model_path(42).as_posix().endswith("state/rl_logs/sector_diversity/PPO_seed_42")


def test_confidence_verdict_requires_trade_count_return_and_profit_factor() -> None:
    assert _confidence_verdict({"trades": 10, "net_pnl": 5000, "profit_factor": 1.2}) == "PASS"
    assert _confidence_verdict({"trades": 9, "net_pnl": 5000, "profit_factor": 1.2}).startswith("FAIL")
