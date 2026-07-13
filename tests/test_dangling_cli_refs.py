"""Tests for code review Finding 3 (2026-07-09):

The auto-burn-in.sh script invokes three CLI commands that no longer
exist in `trading_bot/cli/app.py` after Phase 2.5 cleanup:

- `rl-compare`         (line 413) — fires every 10 burn-in cycles today
                         on the default config; `local` masks errexit so
                         it logs "RL comparison had issues" forever but
                         doesn't crash. RL was intentionally removed.
- `advisory-learn`     (line 518) — gated by advisory.enabled (default false);
                         when enabled, kills the burn-in via `set -e`.
- `advisory-learn --daily-report` (line 531) — already has `|| true` so
                         silent failure; report.md missing when advisory is
                         enabled.

Fix strategy:
- Delete `run_rl_compare` function body + its call site. AGENTS.md
  already says "RL is not in the active burn-in vote path" and
  "The swarm engine is no longer in the automated scan/vote path."
- For advisory: guard `run_advisory_learner` so it logs a one-line
  "advisory-learn is no longer wired" notice and returns 0; same for
  on_shutdown's daily-report call. This keeps the burn-in alive even
  if someone enables advisory (matches AGENTS.md's "opt-in" framing).

The validator confirmed the advisory module is intentionally dormant
on the burn-in path: AGENTS.md frames advisory as a research lane,
not part of the active vote path.
"""

from __future__ import annotations

from pathlib import Path
import shlex

from trading_bot.cli.app import app


SCRIPT = Path(__file__).parent.parent / "scripts" / "auto-burn-in.sh"
AGENTS = Path(__file__).parent.parent / "AGENTS.md"


def test_run_rl_compare_function_removed() -> None:
    """The run_rl_compare function body must be removed entirely.

    RL was intentionally deleted in Phase 2.5 cleanup; the function
    body is dead code that calls a removed CLI command every 10 cycles.
    """
    content = SCRIPT.read_text(encoding="utf-8")
    assert "run_rl_compare()" not in content, (
        "run_rl_compare() must be deleted; AGENTS.md says RL is not in the active burn-in vote path"
    )
    # The recurring warning should also be gone
    assert "RL comparison had issues" not in content


def test_run_rl_compare_call_removed() -> None:
    """The `run_rl_compare` call in the main loop must be removed."""
    content = SCRIPT.read_text(encoding="utf-8")
    # The main loop calls it inside the every-10-cycles block.
    # After the fix, neither the function definition nor the call
    # should appear anywhere in the script.
    assert content.count("run_rl_compare") == 0


def test_run_advisory_learner_does_not_call_removed_cli() -> None:
    """The run_advisory_learner function must NOT invoke the removed CLI command.

    Phase 2.5 cleanup removed the `advisory-learn` CLI command but the
    script still calls it. With default config (advisory.enabled=false),
    this is unreachable. But when an operator opts in (AGENTS.md says
    it is opt-in), line 518 becomes a script-killer under `set -e`.
    """
    content = SCRIPT.read_text(encoding="utf-8")
    assert not _has_tradebot_local_invocation(content, "advisory-learn"), (
        "no `./tradebot-local ... advisory-learn` invocation may remain"
    )
    # The function still exists, but its body must not actually shell out to advisory-learn
    assert "run_advisory_learner" in content
    al_start = content.find("run_advisory_learner() {")
    assert al_start > 0, "run_advisory_learner function must still be defined"
    al_end = content.find("\n}\n", al_start)
    al_body = content[al_start:al_end]
    assert not _has_tradebot_local_invocation(al_body, "advisory-learn"), (
        "run_advisory_learner() body must not invoke the removed CLI command"
    )


def test_advisory_learner_logs_skip_notice() -> None:
    """When advisory-learn is not wired, log a notice so operators see the change."""
    content = SCRIPT.read_text(encoding="utf-8")
    al_start = content.find("run_advisory_learner() {")
    al_end = content.find("\n}\n", al_start)
    al_body = content[al_start:al_end]
    stripped = _strip_bash_comments(al_body).lower()
    assert not _has_tradebot_local_invocation(al_body, "advisory-learn")
    # A short notice message should be present
    assert "no longer wired" in stripped or "not wired" in stripped or "not invoked" in stripped or "phase 2.5" in stripped, (
        "run_advisory_learner() should log a notice explaining advisory-learn is not wired"
    )


def _strip_bash_comments(text: str) -> str:
    """Remove bash comment lines (lines whose first non-whitespace is #)."""
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#"))


def _has_tradebot_local_invocation(text: str, command: str) -> bool:
    """Check whether `text` contains a `./tradebot-local ... <command>` invocation.

    Catches actual command executions, not comment mentions or echo strings.
    """
    import re
    pattern = re.compile(
        r"\.\/tradebot-local[^\n]*\b" + re.escape(command) + r"\b",
        re.MULTILINE,
    )
    return bool(pattern.search(text))


def test_agents_tradebot_commands_are_registered() -> None:
    registered = {
        command.name or command.callback.__name__.replace("_", "-")
        for command in app.registered_commands
        if command.callback is not None
    }
    documented: set[str] = set()

    for raw_line in AGENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("./tradebot-local ") or "<command>" in line:
            continue
        tokens = shlex.split(line, comments=True)
        index = 1
        while index < len(tokens) and tokens[index].startswith("--"):
            if tokens[index] == "--config-path":
                index += 2
            else:
                index += 1
        if index < len(tokens):
            documented.add(tokens[index])

    missing = sorted(documented - registered)
    assert missing == [], f"AGENTS.md advertises unregistered commands: {missing}"
