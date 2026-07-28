"""Render mined patterns into a digest and persist to research DB."""
import json
import logging
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from trading_bot.research.store import ResearchStore
from trading_bot.research.models import Hypothesis, HypothesisCategory, HypothesisStatus

logger = logging.getLogger(__name__)

def generate_digest(
    patterns: list[dict[str, Any]],
    output_dir: Path,
    research_db_path: str = "state/research.db"
) -> None:
    """Generate markdown/JSON digests and write to research DB.

    Args:
        patterns: List of pattern dictionaries from mine_patterns
        output_dir: Where to write digest.md and digest.json
        research_db_path: Path to the SQLite research DB
    """
    if not patterns:
        logger.info("No patterns to digest.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    # Sort patterns by win rate descending
    patterns.sort(key=lambda x: x["win_rate"], reverse=True)

    # 1. Write JSON
    json_path = output_dir / "digest.json"
    with open(json_path, "w") as f:
        json.dump(patterns, f, indent=2)

    # 2. Write Markdown
    md_path = output_dir / "digest.md"
    with open(md_path, "w") as f:
        f.write("# Pattern Mining Digest\n\n")
        f.write(f"Generated at: {datetime.now(timezone.utc).isoformat()}\n\n")

        f.write("| Pattern | Occurrences | Win Rate | Avg Next-Day Return |\n")
        f.write("|---|---|---|---|\n")
        for p in patterns:
            name = p["name"]
            hits = p["hits"]
            win_rate = f"{p['win_rate']:.1%}"
            avg_return = f"{p['avg_return']:.2%}"
            f.write(f"| {name} | {hits} | {win_rate} | {avg_return} |\n")

    # 3. Write to Research DB
    store = ResearchStore(db_path=research_db_path)

    for p in patterns:
        # Only record notable patterns (e.g., > 55% win rate and decent hit count)
        if p["win_rate"] > 0.55 and p["hits"] >= 10:
            hyp = Hypothesis(
                title=f"Mined Pattern: {p['name']}",
                description=p["description"],
                category=HypothesisCategory.CUSTOM,
                status=HypothesisStatus.PENDING,
                expected_outcome=f"Expected win rate ~{p['win_rate']:.1%}",
                parameters=p
            )
            store.save_hypothesis(hyp)

    logger.info(f"Wrote digests to {output_dir}")
