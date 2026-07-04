"""Tests for swarm preset definitions."""

from __future__ import annotations

import pytest

from trading_bot.swarm.base import WorkerConfig
from trading_bot.swarm.presets import (
    ALL_PRESETS,
    CRYPTO_DESK,
    FUNDAMENTAL_ANALYSIS_TEAM,
    INVESTMENT_COMMITTEE,
    MACRO_ECONOMICS_TEAM,
    QUANT_DESK,
    RISK_COMMITTEE,
    TECHNICAL_ANALYSIS_PANEL,
    get_preset,
)


class TestGetPreset:
    """Preset retrieval."""

    def test_get_investment_committee(self):
        configs = get_preset("investment_committee")
        assert len(configs) == 4
        names = [c.name for c in configs]
        assert "technical_analyst" in names
        assert "risk_manager" in names

    def test_get_quant_desk(self):
        configs = get_preset("quant_desk")
        assert len(configs) == 4
        names = [c.name for c in configs]
        assert "factor_model" in names
        assert "quant_risk" in names

    def test_get_risk_committee(self):
        configs = get_preset("risk_committee")
        assert len(configs) == 4
        names = [c.name for c in configs]
        assert "var_analyst" in names
        assert "risk_committee_lead" in names

    def test_get_technical_analysis_panel(self):
        configs = get_preset("technical_analysis_panel")
        assert len(configs) == 5
        names = [c.name for c in configs]
        assert "trend_follower" in names
        assert "technical_consensus" in names

    def test_get_fundamental_analysis_team(self):
        configs = get_preset("fundamental_analysis_team")
        assert len(configs) == 4
        names = [c.name for c in configs]
        assert "valuation_expert" in names
        assert "fundamental_consensus" in names

    def test_get_crypto_desk(self):
        configs = get_preset("crypto_desk")
        assert len(configs) == 4
        names = [c.name for c in configs]
        assert "on_chain_analyst" in names
        assert "crypto_risk" in names

    def test_get_macro_economics_team(self):
        configs = get_preset("macro_economics_team")
        assert len(configs) == 4
        names = [c.name for c in configs]
        assert "economic_indicator" in names
        assert "macro_outlook" in names

    def test_unknown_preset_raises(self):
        with pytest.raises(ValueError, match="Unknown preset"):
            get_preset("nonexistent_preset")

    def test_unknown_preset_mentions_available(self):
        with pytest.raises(ValueError, match="investment_committee"):
            get_preset("nonexistent_preset")


class TestAllPresets:
    """ALL_PRESETS registry."""

    def test_all_presets_registered(self):
        assert "investment_committee" in ALL_PRESETS
        assert "quant_desk" in ALL_PRESETS
        assert "risk_committee" in ALL_PRESETS
        assert "technical_analysis_panel" in ALL_PRESETS
        assert "fundamental_analysis_team" in ALL_PRESETS
        assert "crypto_desk" in ALL_PRESETS
        assert "macro_economics_team" in ALL_PRESETS

    def test_all_presets_match_constants(self):
        assert ALL_PRESETS["investment_committee"] == INVESTMENT_COMMITTEE
        assert ALL_PRESETS["quant_desk"] == QUANT_DESK
        assert ALL_PRESETS["risk_committee"] == RISK_COMMITTEE


class TestWorkerDependencies:
    """Worker dependency chains in presets."""

    def test_investment_committee_risk_manager_depends_on_others(self):
        configs = get_preset("investment_committee")
        risk_mgr = next(c for c in configs if c.name == "risk_manager")
        assert "technical_analyst" in risk_mgr.depends_on
        assert "fundamental_analyst" in risk_mgr.depends_on

    def test_quant_desk_quant_risk_depends_on_others(self):
        configs = get_preset("quant_desk")
        quant_risk = next(c for c in configs if c.name == "quant_risk")
        assert "factor_model" in quant_risk.depends_on
        assert "statistical_arb" in quant_risk.depends_on
        assert "ml_predictor" in quant_risk.depends_on

    def test_technical_consensus_depends_on_all_technical(self):
        configs = get_preset("technical_analysis_panel")
        consensus = next(c for c in configs if c.name == "technical_consensus")
        assert "trend_follower" in consensus.depends_on
        assert "mean_reversion" in consensus.depends_on
        assert "volume_analyst" in consensus.depends_on
        assert "pattern_recognizer" in consensus.depends_on

    def test_priority_ordering(self):
        configs = get_preset("investment_committee")
        priorities = {c.name: c.priority for c in configs}
        assert priorities["technical_analyst"] == 1
        assert priorities["risk_manager"] == 2


class TestDirectConstants:
    """Direct access to preset constants."""

    def test_investment_committee_has_four_workers(self):
        assert len(INVESTMENT_COMMITTEE) == 4

    def test_quant_desk_has_four_workers(self):
        assert len(QUANT_DESK) == 4

    def test_risk_committee_has_four_workers(self):
        assert len(RISK_COMMITTEE) == 4

    def test_technical_analysis_panel_has_five_workers(self):
        assert len(TECHNICAL_ANALYSIS_PANEL) == 5

    def test_fundamental_analysis_team_has_four_workers(self):
        assert len(FUNDAMENTAL_ANALYSIS_TEAM) == 4

    def test_crypto_desk_has_four_workers(self):
        assert len(CRYPTO_DESK) == 4

    def test_macro_economics_team_has_four_workers(self):
        assert len(MACRO_ECONOMICS_TEAM) == 4

    def test_all_configs_have_required_fields(self):
        for preset in ALL_PRESETS.values():
            for config in preset:
                assert isinstance(config, WorkerConfig)
                assert config.name
                assert config.preset
                assert isinstance(config.depends_on, list)
                assert isinstance(config.priority, int)
