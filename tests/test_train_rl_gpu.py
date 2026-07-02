from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from scripts.train_rl_gpu import train_single_model


def test_gpu_training_uses_selected_agent_name_for_outputs(monkeypatch, tmp_path: Path) -> None:
    class FakeAgent:
        def __init__(self, config):
            self.config = config

        def train(self):
            return object()

        def save(self, path):
            Path(path).with_suffix(".zip").write_bytes(b"model")

    monkeypatch.setattr("trading_bot.rl.agent.RLAgent", FakeAgent)
    monkeypatch.setattr(
        "scripts.train_rl_gpu.check_gpu",
        lambda: {"device": "cpu", "available": False},
    )

    args = Namespace(
        agent="A2C",
        period="1y",
        interval="1d",
        observer_window=10,
        max_episode_steps=20,
        reward="risk_adjusted",
        reward_scale=100.0,
        timesteps=10,
        learning_rate=3e-4,
        n_epochs=1,
        batch_size=8,
        n_steps=8,
        gamma=0.99,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.01,
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=0,
    )

    result = train_single_model(["AAPL"], seed=7, args=args, output_dir=tmp_path)

    model_dir = tmp_path / "GPU_seed_7"
    assert result["model_path"] == str(model_dir / "A2C_final.zip")
    assert (model_dir / "A2C_final.zip").exists()
    assert (model_dir / "A2C_final_meta.json").exists()
