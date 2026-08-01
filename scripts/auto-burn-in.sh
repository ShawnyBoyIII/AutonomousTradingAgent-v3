#!/bin/bash
# V3 FULLY AUTOMATED Burn-In with a hybrid universe refresh
# This runs discover → scan → trade → manage loop continuously
#
# Scan interval: 300 seconds (5 minutes) during market hours
# Discovery: HYBRID approach
#   - Daily at market open (9:30 AM ET) - ALWAYS
#   - Mid-day (12:00-1:00 PM ET) - ONLY if watchlist < 5 symbols
# Log rotation: Automatic when decision-log.jsonl exceeds 10MB

set -e

# Self-rewrap in caffeinate so the burn-in keeps running when the lid
# is closed or the Mac locks while on AC power. -s prevents system
# sleep, -i prevents idle sleep, -m prevents disk sleep. The exported
# guard makes the inner invocation a no-op so we don't loop forever.
if [[ -z "${_BURN_IN_CAFFEINATED:-}" ]]; then
    if command -v caffeinate >/dev/null 2>&1; then
        export _BURN_IN_CAFFEINATED=1
        exec caffeinate -s -i -m "$0" "$@"
    fi
fi

echo "=========================================="
echo "FULLY AUTOMATED Paper Burn-In (V3 + hybrid universe)"
echo "Date: $(date)"
echo "Hybrid Universe: ENABLED"
echo "=========================================="
echo ""

cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

# --------------------------------------------------------------------- #
# Runtime pin (2026-07-24): when launched through burnin-launcher.sh
# the burner must always exec the wrapper and Python from the
# immutable snapshot, not the live mutable worktree. PIN_DIR is set
# by the launcher and points at the parent dir of the snapshot
# (<pin>/<head_sha>/). When unset, fall back to the live paths so
# manual operators are unaffected.
# --------------------------------------------------------------------- #
if [ -n "${PIN_DIR:-}" ] && [ -x "$PIN_DIR/tradebot-local" ]; then
    PINNED_TRADEBOT="$PIN_DIR/tradebot-local"
    PINNED_PYTHON="$PIN_DIR/.venv/bin/python"
    export PIN_DIR
    cd "$PIN_DIR"
else
    PINNED_TRADEBOT="./tradebot-local"
    PINNED_PYTHON="./.venv/bin/python"
fi

# Configuration
CONFIG_FILE="burn-in-config.yaml"
UNIVERSE_FILE="state/universe.txt"
WATCHLIST_FILE="state/watchlist.txt"
LOG_DIR="logs"
DB_PATH="state/burn_in.db"
LAST_DISCOVER_FILE=".last_discover_date"
STATE_DIR="state"

# 2026-07-10: Burn-in reliability control plane. Write a heartbeat each
# loop iteration and persist PIDs for the doctor --burn-in health checks
# to consume (see trading_bot/health/checks.py).
HEALTH_STATE_DIR="$STATE_DIR/burn_in"
mkdir -p "$HEALTH_STATE_DIR"
echo "$$" > "$HEALTH_STATE_DIR/burn_in.pid"
HEARTBEAT_FILE="$HEALTH_STATE_DIR/heartbeat.json"
EOD_WATCHDOG_PID_FILE="$HEALTH_STATE_DIR/eod_watchdog.pid"
HEALTH_LOG="$LOG_DIR/health.jsonl"

write_heartbeat() {
    local fills="$1" exits="$2" rejects="$3"
    local ts_iso
    ts_iso=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')
    cat > "$HEARTBEAT_FILE" <<EOF
{"ts":"$ts_iso","cycle":$CYCLE_COUNT,"fills":$fills,"exits":$exits,"rejects":$rejects}
EOF
}

# Sidecar monitoring dashboard
# Override with: AUTO_DASHBOARD=false ./scripts/auto-burn-in.sh
#                 DASHBOARD_PORT=9000 ./scripts/auto-burn-in.sh
AUTO_DASHBOARD="${AUTO_DASHBOARD:-true}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
export DASHBOARD_PORT
DASHBOARD_PID=""
DASHBOARD_LOG="$LOG_DIR/dashboard.log"

# EOD data pipeline: nightly download of massive.com S3 flat-files.
# Override with: EOD_DATA_STORE=false ./scripts/auto-burn-in.sh
#                 EOD_FETCH_TIME=HH:MM (default 11:30 — after massive.com publishes)
#                 EOD_FETCH_BACKFILL_DAYS=0
EOD_DATA_STORE="${EOD_DATA_STORE:-true}"
EOD_FETCH_TIME="${EOD_FETCH_TIME:-11:30}"
EOD_FETCH_BACKFILL_DAYS="${EOD_FETCH_BACKFILL_DAYS:-0}"
EOD_FETCH_LOG="$LOG_DIR/eod_data_store.log"
# Per-interval-set marker (C1 2026-07-08): the marker encodes the interval set
# so a 1d backfill does not block a 1m backfill on the same date. The interval
# default mirrors the CLI helper in trading_bot.cli.app._eod_marker_filename.
EOD_INTERVALS="${EOD_INTERVALS:-$($PINNED_PYTHON -c "from pathlib import Path; from trading_bot.config.loader import load_settings; cfg=load_settings(Path('$CONFIG_FILE')); print(','.join(cfg.eod_data_store.intervals))" 2>/dev/null || echo "1d,1m")}"
EOD_INTERVALS_SLUG=$(echo "$EOD_INTERVALS" | tr ',' '\n' | sort | tr '\n' '_' | sed 's/_$//')
EOD_STORE_ROOT="state/data_store"

# Ensure setup exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Creating burn-in configuration..."
    cat > "$CONFIG_FILE" << 'EOF'
app:
  timezone: "America/New_York"
  state_db_path: "state/burn_in.db"
  log_dir: "logs/burn_in"
  portfolio_summary_path: "state/burn_in/portfolio_summary.json"
  scan_results_path: "state/burn_in/scan_results.json"

market_data:
  provider: "yfinance"
  daily_period: "1y"
  intraday_period: "5d"
  intraday_interval: "5m"
  max_data_age_hours: 72
  max_data_age_minutes: 30
  validate_data: true

risk:
  max_risk_per_trade_pct: 0.01
  max_daily_risk_pct: 0.03
  max_daily_orders: 3
  max_ticker_allocation_pct: 0.20
  min_reward_risk_ratio: 2.0
  use_atr_sizing: true
  max_portfolio_heat_pct: 0.03

session:
  eod_enabled: true
  eod_minutes_before_close: 5

paper:
  fee_per_order: 1.0
  slippage_bps: 0

strategy:
  use_v3_signals: true
  risk_tolerance: "medium"
  min_confidence: "medium"

counter_thesis:
  enabled: true
  block_on_severity: "high"
  aggregate_block_threshold: 0.6
  exit_on_block: true

swarm:
  enabled: true
  preset: "investment_committee"
  max_workers: 4
EOF
fi

# Create default symbols file if missing (will be overwritten by discover)
if [ ! -f "$UNIVERSE_FILE" ]; then
    echo "Creating initial symbol universe..."
    cat > "$UNIVERSE_FILE" << 'EOF'
SPY
QQQ
AAPL
MSFT
NVDA
EOF
fi

# Create log directory
mkdir -p "$LOG_DIR"

# Function to rotate logs if they exceed 10MB
rotate_logs() {
    local max_size=10485760  # 10MB in bytes
    local decision_log="$LOG_DIR/decision-log.jsonl"
    local strategy_log="$LOG_DIR/strategy_results.jsonl"
    
    for log_file in "$decision_log" "$strategy_log"; do
        if [ -f "$log_file" ]; then
            local size=$(stat -f%z "$log_file" 2>/dev/null || stat -c%s "$log_file" 2>/dev/null || echo 0)
            if [ "$size" -gt "$max_size" ]; then
                local timestamp=$(date '+%Y%m%d_%H%M%S')
                local backup="${log_file}.${timestamp}"
                mv "$log_file" "$backup"
                gzip "$backup" 2>/dev/null || true
                echo "[$(date '+%H:%M:%S')] 🔄 Rotated $log_file -> ${backup}.gz"
            fi
        fi
    done
}

# Refresh the broad candidate universe once per discovery day. The CLI
# preserves the previous universe when the scout result is too small, so a
# transient provider failure cannot erase the burner's coverage.
run_universe_refresh() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local refresh_output=""
    local refresh_rc=0

    echo "[$timestamp] 🌐 Refreshing hybrid universe..."
    refresh_output=$(sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" build-universe 2>&1) || refresh_rc=$?
    printf '%s\n' "$refresh_output" | head -100 | sed "s/^/[$timestamp]    /"

    if [ "$refresh_rc" -eq 0 ]; then
        echo "[$timestamp] ✅ Hybrid universe refresh complete"
    else
        echo "[$timestamp] ⚠️  Hybrid universe refresh failed (rc=$refresh_rc); preserving existing universe"
    fi
    return 0
}

# Function to run discovery
run_discovery() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local trigger_reason="$1"  # "daily" or "midday"
    
    echo "[$timestamp] 🔍 Running discovery ($trigger_reason refresh)..."
    
    # Preserve manually added watchlist symbols
    local watchlist_symbols=()
    if [ -f "$WATCHLIST_FILE" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line=$(echo "$line" | tr -d '[:space:]')
            if [ -n "$line" ] && [[ ! "$line" =~ ^# ]]; then
                watchlist_symbols+=("$line")
            fi
        done < "$WATCHLIST_FILE"
    fi
    
    # Run discover with export. Capture both stdout and the exit
    # code so we can distinguish "no candidates" (CLI exits 2) from
    # "fresh discoveries" (CLI exits 0 with new symbols).
    # The `|| true` is required: this script runs under `set -e` and
    # the discover CLI exits 2 when 0 candidates pass the screener;
    # without `|| true` the assignment's non-zero subshell return
    # would kill the burner on every empty-discovery day (which the
    # audit's "discovery failure visibility" change made the default
    # case when EOD data hasn't been published yet). Confirmed via
    # `bash -x` trace on 2026-07-27.
    local discover_output discover_rc
    # canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" discover --mode breakout
    discover_output=$(sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" discover --mode breakout --max 50 --export 2>&1) || true
    discover_rc=$?

    # The "0 candidates" warning is the new failure marker (audit
    # follow-up 2026-07-24). When the screener returns 0 results the
    # CLI prints that warning AND exits 2; the existing universe is
    # preserved but discovery should be logged as failed.
    if echo "$discover_output" | grep -q "0 candidates passed discovery"; then
        echo "[$timestamp] ⚠️  Discovery failed: 0 candidates passed screening. Existing universe preserved."
        echo "{\"event\":\"discovery\",\"timestamp\":\"$timestamp\",\"trigger\":\"$trigger_reason\",\"count\":0,\"status\":\"failed\"}" >> "$LOG_DIR/discovery.log"
        date +%Y-%m-%d > "$LAST_DISCOVER_FILE"
        return 0
    fi

    if [ "$discover_rc" -eq 0 ] && echo "$discover_output" | grep -q "Exported"; then
        local count=$(echo "$discover_output" | grep "Exported" | sed 's/.*Exported \([0-9]*\).*/\1/')
        echo "[$timestamp] ✅ Discovered $count symbols"
        
        # Show top candidates
        local symbols=$(echo "$discover_output" | grep "^  [A-Z]" | head -5)
        if [ -n "$symbols" ]; then
            echo "[$timestamp] Top candidates:"
            echo "$symbols" | while read line; do
                echo "[$timestamp]   $line"
            done
        fi
        
        # Merge discovered symbols with watchlist symbols (preserve manual additions)
        local all_symbols=()
        while IFS= read -r line || [ -n "$line" ]; do
            line=$(echo "$line" | tr -d '[:space:]')
            if [ -n "$line" ] && [[ ! "$line" =~ ^# ]]; then
                all_symbols+=("$line")
            fi
        done < "$UNIVERSE_FILE"
        
        # Add watchlist symbols if not already present
        for ws in "${watchlist_symbols[@]}"; do
            local found=0
            for existing in "${all_symbols[@]}"; do
                if [ "$existing" = "$ws" ]; then
                    found=1
                    break
                fi
            done
            if [ $found -eq 0 ]; then
                all_symbols+=("$ws")
            fi
        done
        
        # Update universe file with merged symbols
        printf "%s\n" "${all_symbols[@]}" > "$UNIVERSE_FILE"
        
        # Log discovery event
        echo "{\"event\":\"discovery\",\"timestamp\":\"$timestamp\",\"trigger\":\"$trigger_reason\",\"count\":$count,\"watchlist_preserved\":${#watchlist_symbols[@]}}" >> "$LOG_DIR/discovery.log"
    else
        echo "[$timestamp] ⚠️  Discovery returned no symbols, preserving existing list"
        # Log failed discovery
        echo "{\"event\":\"discovery\",\"timestamp\":\"$timestamp\",\"trigger\":\"$trigger_reason\",\"count\":0,\"status\":\"failed\"}" >> "$LOG_DIR/discovery.log"
    fi
    
    # Update last discover date
    date +%Y-%m-%d > "$LAST_DISCOVER_FILE"
}

# Check if watchlist is running low on symbols
watchlist_is_low() {
    local threshold=${1:-5}  # Default threshold: 5 symbols
    if [ ! -f "$UNIVERSE_FILE" ]; then
        return 0  # No file = definitely low
    fi
    local count=$(wc -l < "$UNIVERSE_FILE" | tr -d ' ')
    if [ "$count" -lt "$threshold" ]; then
        echo "[$(date '+%H:%M:%S')] 📉 Watchlist low: $count symbols (threshold: $threshold)"
        return 0  # Yes, it's low
    fi
    return 1  # No, we have enough
}

# Check if it's mid-day (between 12:00 PM and 1:00 PM ET)
is_midday() {
    local current_hour=$(TZ=America/New_York date +%H)
    local current_min=$(TZ=America/New_York date +%M)
    local current_time=$((10#$current_hour * 60 + 10#$current_min))
    local midday_start=$((12 * 60))      # 12:00 PM
    local midday_end=$((13 * 60))        # 1:00 PM

    if [ "$current_time" -ge "$midday_start" ] && [ "$current_time" -lt "$midday_end" ]; then
        return 0  # Yes, it's midday
    fi
    return 1  # No
}

# Hybrid discovery logic:
# 1. ALWAYS run at market open (first cycle of day)
# 2. Run mid-day (12:00-1:00 PM ET) ONLY if watchlist < 5 symbols
# 3. Otherwise skip
should_discover() {
    if [ ! -f "$LAST_DISCOVER_FILE" ]; then
        echo "[$(date '+%H:%M:%S')] 🔍 Discovery: first run (no history)"
        return 0  # Never run before
    fi
    
    local last_date=$(cat "$LAST_DISCOVER_FILE")
    local today=$(date +%Y-%m-%d)
    
    # Check if new day - always discover at market open
    if [ "$last_date" != "$today" ]; then
        echo "[$(date '+%H:%M:%S')] 🔍 Discovery: new day ($today)"
        return 0  # New day
    fi
    
    # Already discovered today - check if we should run mid-day refresh
    if is_midday; then
        if watchlist_is_low 5; then
            echo "[$(date '+%H:%M:%S')] 🔍 Discovery: mid-day refresh (watchlist low)"
            return 0  # Mid-day refresh needed
        fi
    fi
    
    return 1  # Skip discovery
}

# Sleep until next market open (9:30 AM ET next weekday)
#
# Phase 5 (P5.1): a single `sleep "$sleep_seconds"` for up to 17.5
# hours is paused by macOS Maintenance Sleep despite
# `caffeinate -s -i -m`. CLOCK_MONOTONIC does not advance while the
# process is suspended, so the timer never reaches the target epoch
# and the burner skips an entire trading day.
#
# Fix: replace the single long sleep with a polling loop that
# sleeps in 60-second chunks. Each chunk is short enough that
# macOS does deliver the wakeup signal after a single Maintenance
# Sleep window, so even an overnight suspension resumes the loop.
# Each iteration also writes a heartbeat so a future stall is
# detectable by the doctor command.
sleep_until_market_open() {
    local current_dow current_hour current_min current_time market_open market_close target_epoch
    local chunk wake_time

    while true; do
        # Always operate in ET — the host may be in any timezone (audit
        # item 16). The script's market hours are 9:30-16:00 ET on
        # weekdays; the main-loop gate at the call site enforces the
        # close-time guard.
        current_dow=$(TZ=America/New_York date +%u)  # 1=Monday, 7=Sunday
        current_hour=$(TZ=America/New_York date +%H)
        current_min=$(TZ=America/New_York date +%M)
        current_time=$((10#$current_hour * 60 + 10#$current_min))
        market_open=$((9 * 60 + 30))
        market_close=$((16 * 60))

        local now_epoch=$(date +%s)

        # Fast path: if it is currently within market hours on a
        # weekday, return immediately. Previously the function only
        # checked `current_time >= market_open`, so 9:30 ET itself
        # fell into the "after market open today" branch and slept
        # 24 hours, leaving the burner stuck on its first post-wake
        # cycle. (Found during burner status check 2026-07-23.)
        if [ "$current_dow" -le 5 ] \
            && [ "$current_time" -ge "$market_open" ] \
            && [ "$current_time" -lt "$market_close" ]; then
            write_heartbeat 0 0 0
            return 0
        fi

        if [ "$current_dow" -gt 5 ]; then
            # Weekend: sleep until Monday 9:30 AM
            local days_until_monday=$((8 - current_dow))
            target_epoch=$((now_epoch + days_until_monday * 86400 - current_time * 60 + market_open * 60))
        elif [ "$current_time" -ge "$market_open" ]; then
            # After market open today: sleep until tomorrow 9:30 AM (or Monday if Friday)
            if [ "$current_dow" -eq 5 ]; then
                target_epoch=$((now_epoch + 3 * 86400 - current_time * 60 + market_open * 60))
            else
                target_epoch=$((now_epoch + 86400 - current_time * 60 + market_open * 60))
            fi
        else
            # Pre-market: sleep until 9:30 AM today
            target_epoch=$((now_epoch + (market_open - current_time) * 60))
        fi

        local sleep_seconds=$((target_epoch - now_epoch))
        if [ "$sleep_seconds" -le 0 ]; then
            # Market is now open. Emit a final heartbeat and return.
            write_heartbeat 0 0 0
            return 0
        fi

        # First iteration: announce the plan with the wake target.
        if [ -z "${_PREMARKET_ANNOUNCED:-}" ]; then
            _PREMARKET_ANNOUNCED=1
            wake_time=$(date -r "$target_epoch" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || date -d "@$target_epoch" '+%Y-%m-%d %H:%M:%S %Z')
            echo "[$timestamp] Pre-market: polling until market open at $wake_time ($sleep_seconds sec total, 60s ticks)"
        fi

        # Phase 5 (P5.1): 60-second polling chunks. Each chunk is short
        # enough that a single macOS Maintenance Sleep window cannot
        # stall us indefinitely; if the kernel suspends us we resume
        # at the next kernel wakeup. Write a heartbeat on each tick so
        # a stalled pre-market is visible in the doctor report.
        chunk=$sleep_seconds
        if [ "$chunk" -gt 60 ]; then
            chunk=60
        fi
        sleep "$chunk"
        write_heartbeat 0 0 0
    done
}

# Load symbols function - reads from Python-configured paths (universe + watchlist)
load_symbols() {
    local all_symbols=()
    
    # Read universe file (ranked symbols from build-universe)
    if [ -f "$UNIVERSE_FILE" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line=$(echo "$line" | tr -d '[:space:]')
            if [ -n "$line" ] && [[ ! "$line" =~ ^# ]]; then
                all_symbols+=("$line")
            fi
        done < "$UNIVERSE_FILE"
    fi
    
    # Read watchlist file (manually added symbols)
    if [ -f "$WATCHLIST_FILE" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line=$(echo "$line" | tr -d '[:space:]')
            if [ -n "$line" ] && [[ ! "$line" =~ ^# ]]; then
                # Add if not already in universe
                local found=0
                for existing in "${all_symbols[@]}"; do
                    if [ "$existing" = "$line" ]; then
                        found=1
                        break
                    fi
                done
                if [ $found -eq 0 ]; then
                    all_symbols+=("$line")
                fi
            fi
        done < "$WATCHLIST_FILE"
    fi
    
    # If no symbols found, use defaults
    if [ ${#all_symbols[@]} -eq 0 ]; then
        all_symbols=("SPY" "QQQ" "AAPL" "MSFT" "NVDA")
    fi
    
    # Join symbols with commas
    SYMBOLS=$(IFS=','; echo "${all_symbols[*]}")
}

echo "Configuration:"
echo "  Config: $CONFIG_FILE"
echo "  Symbols File: $UNIVERSE_FILE"
echo "  Database: $DB_PATH"
echo "  Mode: Hybrid universe + V3/V2.5 consensus"
echo ""

# Pre-flight checks
echo "Pre-flight Checks:"
echo "------------------"

# Check local readiness. ponytail: burn-in should not refuse to start just
# because the existing paper ledger had a bad week; runtime commands already
# enforce kill switch and the next check below verifies market data access.
# canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" doctor
if ! sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" doctor > /dev/null 2>&1; then
    echo "❌ Doctor check failed"
    exit 1
fi
echo "✅ Local app ready"

# Check kill switch
# canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" kill-switch
if ! sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" kill-switch > /dev/null 2>&1; then
    echo "⚠️  Kill switch is ACTIVE - cannot start"
    echo "Resume with: sh $PINNED_TRADEBOT kill-switch --resume"
    exit 1
fi
echo "✅ Kill switch: Trading active"

# Test scan (use any known valid symbol)
echo "Testing market connection..."
# canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" scan --symbols SPY
if ! sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" scan --symbols SPY --summary > /dev/null 2>&1; then
    echo "❌ Market connection failed"
    exit 1
fi
echo "✅ Market data accessible"
echo ""

echo "=========================================="
echo "Starting FULLY AUTOMATED Burn-In"
echo ""
echo "This will:"
echo "  1. Discover new candidates on first cycle of each day"
echo "  2. Scan universe every 60 seconds during market hours"
echo "  3. Auto-trade GREEN signals (V3 + V2.5 consensus; RL disabled in burn-in)"
echo "  4. Manage positions (stops, targets, EOD)"
echo "  5. Log everything to $LOG_DIR"
echo ""
echo "To monitor:"
echo "  Dashboard:   http://127.0.0.1:$DASHBOARD_PORT  (auto-started)"
echo "  New terminal: sh ./scripts/burn-in-monitor.sh"
echo "  Live log:    tail -f $LOG_DIR/decision-log.jsonl"
echo "  Dashboard log: tail -f $DASHBOARD_LOG"
echo ""
echo "To stop: Press Ctrl-C"
echo "=========================================="
echo ""

# Function to run the pattern miner pass
run_pattern_miner() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] 🔎 Running pattern miner..."

    local output
    if ! output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" pattern-mine 2>&1); then
        echo "[$timestamp] ⚠️  Pattern miner failed:"
        echo "$output"
        return 0  # Do not kill burn-in on miner failure
    fi

    echo "[$timestamp] ✅ Pattern miner complete"
    return 0
}

# Function to refresh tuning overrides from recent paper results
run_nightly_tuning() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] 🧠 Running nightly tuning..."

    run_tune_experiment_step
    return 0
}

run_tune_experiment_step() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local eval_log="$LOG_DIR/tune_experiment.log"

    set +e
    # canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" tune-experiment evaluate
    eval_output=$(sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" tune-experiment evaluate 2>&1)
    local eval_rc=$?
    set -e

    if [ $eval_rc -ne 0 ]; then
        # No active experiment (exit 2) is normal — fall through to propose.
        if [ $eval_rc -ne 2 ]; then
            echo "[$timestamp] ⚠️  tune-experiment evaluate exit=$eval_rc (see $eval_log)"
            echo "$eval_output" >> "$eval_log"
        fi
    fi

    set +e
    # canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" tune-experiment propose
    propose_output=$(sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" tune-experiment propose 2>&1)
    local propose_rc=$?
    set -e

    if [ $propose_rc -ne 0 ]; then
        echo "[$timestamp] ⚠️  tune-experiment propose exit=$propose_rc (see $eval_log)"
        echo "$propose_output" >> "$eval_log"
    fi

    # Manual `tune` is now gated by an active experiment. We skip running
    # `tune` here because the controller owns this nightly step.
}

# Function to run the burn-in reliability health check.
# Mirrors run_nightly_tuning(): capture stdout to a log, never exit 1 on
# failure (the heartbeats themselves expose health; we don't want the
# check pipeline to take down the burn-in).
run_health_check() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local rc=0
    local output
    # canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" doctor --burn-in
    # Forward PIN_DIR explicitly so the subprocess's loader sees the
    # snapshot root for burn_in.pid / heartbeat / scan_results /
    # dashboard.port lookups, even if a future refactor drops the
    # top-level ``export PIN_DIR`` from this script.
    output=$(PIN_DIR="$PIN_DIR" sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" doctor --burn-in --json 2>&1) || rc=$?
    echo "$output" >> "$HEALTH_LOG" 2>/dev/null || true
    if [ "$rc" -ne 0 ]; then
        echo "[$timestamp] ⚠️  Health check exit=$rc (see $HEALTH_LOG)"
    fi
    return 0
}

# Function to fetch EOD data from massive.com S3 into the long-term store.
# Idempotent per interval set (C1 2026-07-08): skip only when a marker for
# THIS interval set exists for today's date — a 1d backfill does not block
# a 1m backfill on the same date.
run_eod_data_download() {
    if [ "$EOD_DATA_STORE" != "true" ]; then
        return 0
    fi
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    # Gate on time window — wait for massive.com to publish (≈11:00 ET) plus a buffer.
    # Always evaluate against ET; the host may be in another timezone.
    local current_time=$(TZ=America/New_York date '+%H:%M')
    if [ "$current_time" \< "$EOD_FETCH_TIME" ]; then
        return 0
    fi

    local today_ymd=$(TZ=America/New_York date '+%Y-%m-%d')
    # Per-interval marker path matches the CLI helper convention:
    # ".last_eod_fetch_<YYYY-MM-DD>_<sorted_intervals>.marker".
    local marker="${EOD_STORE_ROOT}/.last_eod_fetch_${today_ymd}_${EOD_INTERVALS_SLUG}.marker"

    # Skip if a marker for THIS interval set exists.
    if [ -f "$marker" ]; then
        echo "[$timestamp] Ⓜ️  EOD already fetched for $today_ymd intervals=$EOD_INTERVALS_SLUG (skipping)"
        return 0
    fi

    echo "[$timestamp] 📥 Fetching EOD data (target=$today_ymd, intervals=$EOD_INTERVALS_SLUG, backfill=$EOD_FETCH_BACKFILL_DAYS days)..."

    local fetch_output
    # Rely on the CLI default (previous trading day in America/New_York):
    # the CLI's `_previous_trading_day` helper skips weekends and honors
    # ET, so a Monday-evening fetch lands on Friday. A shell-side host
    # calendar override mis-targets the S3 partition around DST
    # transitions or weekend rollover and yields a 404. Operators can
    # still pass --date explicitly to override.
    # canonical: ./tradebot-local --config-path "$CONFIG_FILE" eod-fetch
    fetch_output=$(sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" eod-fetch \
        --backfill-days "$EOD_FETCH_BACKFILL_DAYS" 2>&1)
    local status=$?

    echo "$fetch_output" >> "$EOD_FETCH_LOG"
    if [ $status -ne 0 ]; then
        echo "[$timestamp] ⚠️  EOD fetch failed (see $EOD_FETCH_LOG); continuing"
        return 0
    fi

    # Only mark complete if the CLI reported at least one partition written
    # (avoids idempotency lock-out when the network is down — see code review).
    if echo "$fetch_output" | grep -q "total_partitions=0$"; then
        echo "[$timestamp] ⚠️  EOD fetch returned zero partitions; not marking complete (will retry)"
        return 0
    fi

    mkdir -p "$(dirname "$marker")"
    echo "$today_ymd" > "$marker"
    echo "[$timestamp] ✅ EOD fetch complete (marker: $(basename "$marker"), see $EOD_FETCH_LOG)"
    return 0
}

# Function to refresh advisory learner artifacts
run_advisory_learner() {
    # 2026-07-09 review fix: `advisory-learn` was removed in Phase 2.5
    # cleanup. The function used to invoke the removed CLI command every
    # 10 cycles; under `set -e` that kills the burn-in when an operator
    # enables `advisory.enabled`. Log a one-line notice and return 0 so
    # the main loop is unaffected.  AGENTS.md frames advisory as a
    # research lane, not part of the active burn-in vote path.
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] ℹ️  advisory-learn is no longer wired into the burn-in (Phase 2.5 cleanup)"
    return 0
}

on_shutdown() {
    stop_eod_watchdog
    stop_dashboard
    # 2026-07-09 review fix: same — advisory-learn was removed in
    # Phase 2.5; the prior `|| true` made this a silent failure. Now
    # we skip cleanly without invoking the missing command.
    if [ "$ADVISORY_ENABLED" != "true" ]; then
        return 0
    fi
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] ℹ️  advisory-learn --daily-report is no longer wired (Phase 2.5 cleanup); skipping"
    return 0
}

# --- Monitoring dashboard sidecar -----------------------------------------
# Start a sidecar dashboard process so the user can monitor the burn-in in a
# browser at http://127.0.0.1:$DASHBOARD_PORT. The dashboard is read-only and
# binds to localhost; it never affects trading.

ensure_dashboard() {
    if [ "$AUTO_DASHBOARD" != "true" ]; then
        return 0
    fi
    # Already running?
    if [ -n "$DASHBOARD_PID" ] && kill -0 "$DASHBOARD_PID" 2>/dev/null; then
        return 0
    fi

    if [ ! -x "./scripts/start-dashboard.sh" ]; then
        echo "[$(date '+%H:%M:%S')] ⚠️  scripts/start-dashboard.sh missing — skipping dashboard sidecar"
        return 0
    fi

    # 2026-07-09 fix: detect and clear orphan port holders (e.g. a previous
    # session's uvicorn whose listener died but the process is still alive).
    # A 1-second health check is enough to distinguish a real dashboard
    # (responds fast) from a zombie (no listener / refused connection).
    if command -v lsof >/dev/null 2>&1 && lsof -ti :"$DASHBOARD_PORT" >/dev/null 2>&1; then
        local timestamp=$(date '+%H:%M:%S')
        # Try a quick health check first
        if ! curl -sf -m 1 "http://127.0.0.1:$DASHBOARD_PORT/api/health" >/dev/null 2>&1; then
            echo "[$timestamp] 🧹 Found orphan on port $DASHBOARD_PORT (no /api/health response); clearing"
            local orphan_pids=$(lsof -ti :"$DASHBOARD_PORT" 2>/dev/null)
            if [ -n "$orphan_pids" ]; then
                echo "$orphan_pids" | xargs kill -9 2>/dev/null || true
                sleep 0.5
            fi
        else
            echo "[$timestamp] ⚠️  Port $DASHBOARD_PORT already serves a healthy dashboard; skipping sidecar"
            DASHBOARD_PID=""
            # Persist the sidecar port so manual operators running
            # ``doctor --burn-in`` from outside the burner can discover
            # the actual dashboard port without exporting env vars.
            printf '%s\n' "$DASHBOARD_PORT" > "$HEALTH_STATE_DIR/dashboard.port"
            return 0
        fi
    fi

    echo "[$(date '+%H:%M:%S')] 📊 Starting monitoring dashboard on port $DASHBOARD_PORT..."
    CONFIG_PATH="$CONFIG_FILE" ./scripts/start-dashboard.sh \
        --config "$CONFIG_FILE" \
        --port "$DASHBOARD_PORT" \
        > "$DASHBOARD_LOG" 2>&1 &
    DASHBOARD_PID=$!

    # Wait briefly for it to bind (or fail).
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "$DASHBOARD_PID" 2>/dev/null; then
            echo "[$(date '+%H:%M:%S')] ⚠️  Dashboard exited during startup (see $DASHBOARD_LOG); continuing without it"
            DASHBOARD_PID=""
            return 0
        fi
        if grep -q "Uvicorn running" "$DASHBOARD_LOG" 2>/dev/null; then
            break
        fi
        sleep 0.3
    done

    if kill -0 "$DASHBOARD_PID" 2>/dev/null; then
        echo "[$(date '+%H:%M:%S')] ✅ Dashboard live: http://127.0.0.1:$DASHBOARD_PORT  (pid=$DASHBOARD_PID, log=$DASHBOARD_LOG)"
        # Persist the sidecar port so manual operators running
        # ``doctor --burn-in`` from outside the burner can discover it.
        printf '%s\n' "$DASHBOARD_PORT" > "$HEALTH_STATE_DIR/dashboard.port"
    else
        echo "[$(date '+%H:%M:%S')] ⚠️  Dashboard did not become ready; continuing without it (see $DASHBOARD_LOG)"
        DASHBOARD_PID=""
        rm -f "$HEALTH_STATE_DIR/dashboard.port"
    fi
}

stop_dashboard() {
    if [ -z "$DASHBOARD_PID" ]; then
        return 0
    fi
    if ! kill -0 "$DASHBOARD_PID" 2>/dev/null; then
        DASHBOARD_PID=""
        return 0
    fi
    echo "[$(date '+%H:%M:%S')] 📊 Stopping dashboard (pid=$DASHBOARD_PID)..."
    kill "$DASHBOARD_PID" 2>/dev/null || true
    # Give it a moment to exit cleanly.
    for _ in 1 2 3 4 5; do
        if ! kill -0 "$DASHBOARD_PID" 2>/dev/null; then
            DASHBOARD_PID=""
            rm -f "$HEALTH_STATE_DIR/dashboard.port"
            return 0
        fi
        sleep 0.3
    done
    kill -9 "$DASHBOARD_PID" 2>/dev/null || true
    DASHBOARD_PID=""
    rm -f "$HEALTH_STATE_DIR/dashboard.port"
}

# --------------------------------------------------------------------- #
# manage-positions concurrency lock (2026-07-09 review fix)
# --------------------------------------------------------------------- #
# The EOD watchdog and the main burn-in loop can both call
# `./tradebot-local manage-positions` at 15:55 ET.  The watchdog
# polls every 30s and the main loop runs manage-positions on each
# 60s cycle, so the overlap window is real (15:55-16:00 daily).  The
# `ledger.record_fill` INSERT uses fresh UUIDs (no PK conflict), so
# duplicate calls produce duplicate SELL rows that double-count
# realized P&L and corrupt the profit-factor graduation gate.
#
# `flock` is not available on macOS (this is the burn-in host).
# `mkdir` is atomic on macOS HFS+/APFS and Linux ext4, so we use
# directory creation as the lock primitive.  The PID is stored inside
# the lock directory so a crashed lock-holder's lock can be reclaimed.
_MANAGE_LOCK_DIR="state/.manage.lock"
_manage_lock_acquire() {
    if [ -d "$_MANAGE_LOCK_DIR" ]; then
        local holder_pid=$(cat "$_MANAGE_LOCK_DIR/pid" 2>/dev/null)
        if [ -n "$holder_pid" ] && kill -0 "$holder_pid" 2>/dev/null; then
            return 1  # held by a live process — caller must skip
        fi
        # Stale lock (holder crashed, SIGKILL'd, or OOM'd). Reclaim it.
        rm -rf "$_MANAGE_LOCK_DIR"
    fi
    mkdir "$_MANAGE_LOCK_DIR" 2>/dev/null || return 1
    echo $$ > "$_MANAGE_LOCK_DIR/pid"
    return 0
}

_manage_lock_release() {
    # Only remove if WE hold the lock (PID matches), so a slow caller
    # never wipes out a successor's lock.
    if [ -f "$_MANAGE_LOCK_DIR/pid" ] && [ "$(cat "$_MANAGE_LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then
        rm -rf "$_MANAGE_LOCK_DIR"
    fi
}

# --------------------------------------------------------------------- #
# EOD-exit watchdog (2026-07-09 fix)
# --------------------------------------------------------------------- #
# 2026-07-09: the burn-in's main loop hung for 7+ hours at the Polygon
# scan step, blocking the 15:55 ET EOD exit.  This watchdog runs in a
# background subshell so it survives even when the main loop is hung.
# It polls every 30 seconds, fires once at 15:55 ET on weekdays (using
# a marker file for idempotency), and stops cleanly on shutdown.
EOD_WATCHDOG_PID=""
start_eod_watchdog() {
    local config_file="$CONFIG_FILE"
    local state_dir="state"
    local eod_minute=$((15 * 60 + 55))  # 15:55 ET
    (
        while true; do
            local now_h=$(TZ=America/New_York date +%H)
            local now_m=$(TZ=America/New_York date +%M)
            local now_dow=$(TZ=America/New_York date +%u)  # 1=Mon..7=Sun
            local today=$(TZ=America/New_York date +%Y-%m-%d)
            local now_min=$((10#$now_h * 60 + 10#$now_m))
            local marker="$state_dir/.last_eod_watchdog_fire_${today}.marker"

            if [ "$now_dow" -le 5 ] \
                && [ "$now_min" -ge "$eod_minute" ] \
                && [ ! -f "$marker" ]; then
                local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
                echo "[$timestamp] 🛡️  EOD watchdog firing (15:55 ET safety net)"
                # 2026-07-09 review fix: capture output + exit code separately
                # so a failing manage-positions does NOT touch the marker.
                # Without this, the pipeline `| head | sed` returns sed's exit
                # code (always 0), masking any upstream failure and silently
                # marking the day "complete" — blocking retry for the rest
                # of the day and leaving EOD positions open past close.
                #
                # 2026-07-09 review fix #2: serialize against the main loop
                # via _manage_lock_acquire/release. If the main loop is
                # currently running manage-positions, skip this iteration
                # and mark the day complete (the main loop is handling it).
                # Without this, both processes can write duplicate SELL
                # orders with distinct UUIDs (no PK conflict) — corrupting
                # realized P&L and the profit-factor graduation gate.
                local eod_output eod_rc eod_status
                if _manage_lock_acquire; then
                    # canonical: sh ./tradebot-local --config-path "$config_file" manage-positions
                    eod_output=$(sh ./tradebot-local --config-path "$config_file" manage-positions 2>&1)
                    eod_rc=$?
                    _manage_lock_release
                    printf '%s\n' "$eod_output" | head -100 | sed "s/^/[$timestamp]    /"
                    if [ "$eod_rc" -eq 0 ]; then
                        eod_status="ok"
                    else
                        eod_status="retry"
                    fi
                else
                    # Lock contention: the main loop is currently running
                    # manage-positions. Retry on the next 30s poll — do NOT
                    # mark the day complete yet. The previous "mark complete
                    # on contention" path silently swallowed EOD exits when
                    # the main loop was just starting or stuck.
                    echo "[$timestamp] ℹ️  manage-positions already running (main loop?), EOD watchdog will retry; marker NOT yet created"
                    eod_status="lock_contention"
                fi
                if [ "$eod_status" = "ok" ]; then
                    touch "$marker"
                    echo "[$timestamp] ✅ EOD watchdog complete (marker=$marker)"
                else
                    # Marker intentionally NOT touched — the next 30s poll
                    # will retry. This prevents losing the EOD exit to a
                    # transient failure (db lock, etc.) or to a hung CLI
                    # that returned 0 without actually managing positions.
                    echo "[$timestamp] ⚠️  EOD watchdog manage-positions FAILED (rc=$eod_rc, status=$eod_status); marker NOT created, will retry next cycle"
                fi
            fi
            sleep 30
        done
    ) &
    EOD_WATCHDOG_PID=$!
    echo "$EOD_WATCHDOG_PID" > "$EOD_WATCHDOG_PID_FILE" 2>/dev/null || true
    echo "[$(date '+%H:%M:%S')] 🛡️  EOD watchdog started (pid=$EOD_WATCHDOG_PID, fires daily at 15:55 ET)"
}

stop_eod_watchdog() {
    if [ -z "$EOD_WATCHDOG_PID" ]; then
        return 0
    fi
    if kill -0 "$EOD_WATCHDOG_PID" 2>/dev/null; then
        echo "[$(date '+%H:%M:%S')] 🛡️  Stopping EOD watchdog (pid=$EOD_WATCHDOG_PID)..."
        kill "$EOD_WATCHDOG_PID" 2>/dev/null || true
    fi
    EOD_WATCHDOG_PID=""
}

# Boot the sidecar monitoring dashboard (idempotent; respects AUTO_DASHBOARD).
# Called AFTER ensure_dashboard is defined above.
ensure_dashboard
start_eod_watchdog
run_health_check   # one-time baseline on boot

# Function to scan and trade
scan_and_trade() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local symbol_count=$(echo "$SYMBOLS" | tr ',' '\n' | wc -l)
    echo "[$timestamp] Scanning $symbol_count symbols..."
    
    # Run scan and capture output
    # canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" scan --symbols "$SYMBOLS"
    local scan_output=$(sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" scan --symbols "$SYMBOLS" --why --summary 2>&1)
    local scan_summary=$(echo "$scan_output" | grep '^summary ' | tail -1)
    if [ -n "$scan_summary" ]; then
        echo "[$timestamp] 📋 Scan summary: $scan_summary"
    fi
    
    # Check if kill switch is active
    if echo "$scan_output" | grep -q "KILL_SWITCH"; then
        echo "[$timestamp] ⚠️  Kill switch active - skipping trade cycle"
        return 0
    fi
    
    # Extract GREEN symbols (approved with quality=GREEN)
    # Format: SYMBOL APPROVED quality=GREEN ...
    local green_symbols=$(echo "$scan_output" | grep "APPROVED.*quality=GREEN" | awk '{print $1}' | tr '\n' ',' | sed 's/,$//')
    
    if [ -n "$green_symbols" ]; then
        echo "[$timestamp] 🟢 GREEN signals detected: $green_symbols"
        echo "[$timestamp] Executing paper trades..."
        
        # Trade each GREEN symbol
        IFS=',' read -ra SYMBOL_ARRAY <<< "$green_symbols"
        for symbol in "${SYMBOL_ARRAY[@]}"; do
            # Check daily limits first
            # canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" paper-trade --symbols "$symbol"
            local trade_output=$(sh "$PINNED_TRADEBOT" --config-path "$CONFIG_FILE" paper-trade --symbols "$symbol" 2>&1)
            
            if echo "$trade_output" | grep -q "FILLED"; then
                echo "[$timestamp] ✅ Filled: $symbol"
            elif echo "$trade_output" | grep -q "REJECTED"; then
                local reason=$(echo "$trade_output" | grep "REJECTED" | head -1)
                echo "[$timestamp] ❌ Rejected: $symbol - $reason"
            elif echo "$trade_output" | grep -q "NO_SIGNAL"; then
                # A3 (2026-07-08): parse the actual reason from paper-trade output
                # instead of the misleading hardcoded "stale data" label. The
                # paper-trade CLI prints "NO_SIGNAL reason=<text>"; extract that.
                local nosig_reason=$(echo "$trade_output" \
                    | grep "NO_SIGNAL" | head -1 \
                    | sed 's/.*reason=//' \
                    | awk '{print $1}' \
                    | sed 's/[;,].*//')
                [ -z "$nosig_reason" ] && nosig_reason="unknown"
                echo "[$timestamp] ⚪ No signal: $symbol (reason=$nosig_reason)"
            fi
        done
    else
        echo "[$timestamp] ⚪ No GREEN signals"
    fi
    
    # Always run manage-positions to check stops/targets/EOD
    # 2026-07-09 review fix: serialize with the EOD watchdog via
    # _manage_lock_acquire/release. If the watchdog currently holds
    # the lock, skip this cycle (the watchdog handles EOD exit);
    # the next main-loop iteration will pick up slack management.
    echo "[$timestamp] Managing positions..."
    local manage_output=""
    if _manage_lock_acquire; then
        # canonical: sh ./tradebot-local --config-path "$CONFIG_FILE" manage-positions
        manage_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" manage-positions 2>&1)
        _manage_lock_release
    else
        echo "[$timestamp] ℹ️  manage-positions already running (EOD watchdog?), main loop skipping"
    fi
    
    # Log summary
    if echo "$manage_output" | grep -q "actions=0"; then
        echo "[$timestamp] ✓ No position actions needed"
    else
        # Extract and display actions
        local summary=$(echo "$manage_output" | grep "positions=" | head -1)
        echo "[$timestamp] 📊 $summary"
        
        # Show any fills
        local fills=$(echo "$manage_output" | grep "FILLED")
        if [ -n "$fills" ]; then
            echo "[$timestamp] 💰 Fills:"
            echo "$fills" | while read line; do
                echo "[$timestamp]    $line"
            done
        fi
    fi
}

# Function to check confidence gates before trading
check_confidence_gates() {
    local db_path="$1"
    local min_trades=${2:-10}
    local min_net_pnl=${3:-500}
    
    if [ ! -f "$db_path" ]; then
        echo "[$(date '+%H:%M:%S')] ⚠️  Confidence gates: no database yet, skipping gates"
        return 0
    fi
    
    local result=$($PINNED_PYTHON -c "
import sqlite3
import json
import sys

db_path = '$db_path'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute('SELECT COUNT(*) FROM trades WHERE exit_price IS NOT NULL')
total_trades = cursor.fetchone()[0]

cursor.execute('SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE exit_price IS NOT NULL')
realized_pnl = cursor.fetchone()[0]

conn.close()

# Check gates
min_trades = $min_trades
min_net_pnl = $min_net_pnl

gates_passed = True
issues = []

if total_trades < min_trades:
    gates_passed = False
    issues.append(f'trades={total_trades}<{min_trades}')

if realized_pnl < min_net_pnl:
    gates_passed = False
    issues.append(f'pnl={realized_pnl:.0f}<{min_net_pnl}')

if gates_passed:
    print('GATES_PASSED')
else:
    print(f'GATES_FAILED: {\", \".join(issues)}')
" 2>/dev/null)
    
    if echo "$result" | grep -q "GATES_PASSED"; then
        echo "[$(date '+%H:%M:%S')] ✅ Confidence gates passed"
        return 0
    elif echo "$result" | grep -q "GATES_FAILED"; then
        echo "[$(date '+%H:%M:%S')] ⚠️  Confidence gates NOT met: $result"
        return 1
    else
        echo "[$(date '+%H:%M:%S')] ⚠️  Could not evaluate confidence gates"
        return 0
    fi
}

# Function to check max drawdown and halt if exceeded.
# Delegates to the Python implementation so the shell shares the
# cohort-aware drawdown contract (paper.equity_evaluation_since).
check_max_drawdown() {
    local db_path="$1"
    local max_drawdown_pct=${2:-10}  # Default: 10% max drawdown

    if [ ! -f "$db_path" ]; then
        echo "[$(date '+%H:%M:%S')] ⚠️  Max drawdown check: no database yet, skipping"
        return 0
    fi

    local result
    result=$($PINNED_PYTHON -c "
from datetime import datetime, timezone
from pathlib import Path
from trading_bot.config.loader import load_settings
from trading_bot.monitoring.drawdown import compute_drawdown_from_ledger
from trading_bot.portfolio.ledger import PortfolioLedger

db_path = Path('$db_path')
cfg = load_settings(Path('$CONFIG_FILE'))
boundary = (
    getattr(cfg.paper, 'equity_evaluation_since', None)
    or getattr(cfg.paper, 'graduation_since', None)
)
ledger = PortfolioLedger(db_path)
metrics = compute_drawdown_from_ledger(
    ledger,
    limit=None,
    since=boundary,
    naive_timezone=cfg.app.timezone,
)
if not metrics.sufficient_evidence:
    print(f'DRAWDOWN_OK:insufficient_evidence,boundary={boundary.isoformat() if boundary else None}')
    raise SystemExit(0)

max_dd_limit = $max_drawdown_pct
peak = metrics.peak_equity
current = metrics.recovery_equity
max_dd = metrics.max_drawdown_pct
total_return = (current / peak - 1) * 100 if peak > 0 else 0.0

if max_dd >= max_dd_limit:
    print(f'DRAWDOWN_HALT:peak_dd={max_dd:.2f}%>={max_dd_limit}%,current_equity={current:.2f},starting_equity={peak:.2f}')
elif max_dd >= max_dd_limit * 0.8:
    print(f'DRAWDOWN_WARNING:peak_dd={max_dd:.2f}% approaching {max_dd_limit}%,current_equity={current:.2f}')
else:
    print(f'DRAWDOWN_OK:peak_dd={max_dd:.2f}%,current_equity={current:.2f},total_return={total_return:.2f}%')
" 2>/dev/null)

    if echo "$result" | grep -q "DRAWDOWN_HALT"; then
        echo "[$(date '+%H:%M:%S')] 🚨 MAX DRAWDOWN HALT: $result"
        echo "[$(date '+%H:%M:%S')] 🚨 Halting burn-in - drawdown exceeded ${max_drawdown_pct}%"
        echo "[$(date '+%H:%M:%S')] Review: tail -f $LOG_DIR/decision-log.jsonl"
        echo "[$(date '+%H:%M:%S')] Resume manually after review: sh ./scripts/auto-burn-in.sh"
        # Log halt event
        echo "{\"event\":\"max_drawdown_halt\",\"timestamp\":\"$(date '+%Y-%m-%d %H:%M:%S')\",\"reason\":\"$result\"}" >> "$LOG_DIR/halt.log"
        exit 1
    elif echo "$result" | grep -q "DRAWDOWN_WARNING"; then
        echo "[$(date '+%H:%M:%S')] ⚠️  Drawdown warning: $result"
        # Log warning but continue
        echo "{\"event\":\"drawdown_warning\",\"timestamp\":\"$(date '+%Y-%m-%d %H:%M:%S')\",\"reason\":\"$result\"}" >> "$LOG_DIR/halt.log"
    else
        echo "[$(date '+%H:%M:%S')] ✅ Drawdown OK: $result"
    fi

    return 0
}

# Main loop
echo "Starting automated loop..."
echo ""

# Track cycle count
CYCLE_COUNT=0

# 2026-07-30 fix: SIGTERM/SIGINT must actually exit the script. The
# on_shutdown handler cleans up the dashboard + EOD watchdog but the
# trap doesn't `exit`, so the loop kept cycling in `sleep 60` after
# SIGTERM and required SIGKILL. Split the trap so signal-triggered
# shutdown calls `exit 0` (after cleanup) while the EXIT trap (normal
# completion via `set -e`) just runs cleanup without overriding the
# exit status. See `kill -TERM` behavior in scripts/burn-in-monitor.sh.
trap 'on_shutdown; exit 0' INT TERM
trap on_shutdown EXIT

# Confidence gate thresholds (advisory: alert but don't block on PF/windows)
MIN_TRADES=50
MIN_NET_PNL=0
# Honor the burn-in config's disabled drawdown circuit breaker by default.
# Operators can still override via env var for one-off sessions.
ENABLE_DRAWDOWN_CIRCUIT_BREAKER=$($PINNED_PYTHON -c "from pathlib import Path; from trading_bot.config.loader import load_settings; s=load_settings(Path('$CONFIG_FILE')); print('true' if s.risk.enable_drawdown_circuit_breaker else 'false')" 2>/dev/null || printf "true")
if [ "$ENABLE_DRAWDOWN_CIRCUIT_BREAKER" = "false" ]; then
    MAX_DRAWDOWN_PCT=${MAX_DRAWDOWN_PCT:-999}
else
    MAX_DRAWDOWN_PCT=${MAX_DRAWDOWN_PCT:-10}
fi
ADVISORY_ENABLED=$($PINNED_PYTHON -c "from pathlib import Path; from trading_bot.config.loader import load_settings; s=load_settings(Path('$CONFIG_FILE')); print('true' if s.advisory.enabled else 'false')" 2>/dev/null || printf "false")

while true; do
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    CYCLE_COUNT=$((CYCLE_COUNT + 1))
    write_heartbeat 0 0 0  # fills/exits/rejects tracked later; placeholder for now

    # EOD ingestion: idempo­tent per (date, intervals). The function itself
    # gates on EOD_FETCH_TIME and a per-interval marker, so calling it on
    # every cycle is safe — it returns immediately when its time gate or
    # marker is unmet. Previously the call lived inside the daily-discovery
    # branch which only fires once per session; if the first cycle ran
    # before 11:30 ET the EOD fetch was skipped for the rest of the day.
    run_eod_data_download

    # Check if we should run discovery (hybrid: daily + conditional mid-day)
    if should_discover; then
        # Determine trigger reason for logging
        if is_midday; then
            run_discovery "midday"
        else
            run_universe_refresh
            run_discovery "daily"
            # nightly tuning runs after discovery; EOD fetch already ran above
            run_pattern_miner
            run_nightly_tuning
        fi
        load_symbols
    fi
    
    # Reload symbols in case file changed
    load_symbols
    
    # Check if market hours (9:30 AM - 4:00 PM ET, weekdays). Use ET
    # explicitly — the host may be in another timezone, and a CDT
    # workstation would otherwise evaluate against local time.
    current_hour=$(TZ=America/New_York date +%H)
    current_min=$(TZ=America/New_York date +%M)
    current_dow=$(TZ=America/New_York date +%u)  # 1=Monday, 7=Sunday
    current_time=$((10#$current_hour * 60 + 10#$current_min))
    market_open=$((9 * 60 + 30))   # 9:30 AM ET
    market_close=$((16 * 60))       # 4:00 PM ET

    # If not market hours, sleep efficiently until next open
    if [ "$current_dow" -gt 5 ] || [ "$current_time" -lt "$market_open" ] || [ "$current_time" -ge "$market_close" ]; then
        sleep_until_market_open
        continue
    fi
    
    # Market is open - run cycle
    scan_and_trade

    # Restart the dashboard sidecar if it died for any reason.
    ensure_dashboard

    # Check drawdown after each cycle
    check_max_drawdown "$DB_PATH" "$MAX_DRAWDOWN_PCT"
    
    # Show cycle summary every 10 cycles
    if [ $((CYCLE_COUNT % 10)) -eq 0 ]; then
        echo "[$timestamp] 📈 Completed $CYCLE_COUNT cycles"

        # Check confidence gates every 10 cycles
        if ! check_confidence_gates "$DB_PATH" "$MIN_TRADES" "$MIN_NET_PNL"; then
            echo "[$timestamp] ⚠️  Confidence gates NOT met: advisory only (alert-only mode)"
            echo "[$timestamp] Trading performance below minimum thresholds"
            echo "[$timestamp] Review: tail -f $LOG_DIR/decision-log.jsonl"
            echo "{\"event\":\"confidence_gate_alert\",\"timestamp\":\"$timestamp\",\"reason\":\"gates_failed\",\"mode\":\"advisory_only\"}" >> "$LOG_DIR/halt.log"
        fi

        run_advisory_learner
    fi

    # 30-minute health check cadence
    if [ $((CYCLE_COUNT % 30)) -eq 0 ]; then
        run_health_check
    fi
    # 15:50 ET pre-EOD hard check (5 min before EOD exit)
    # NOTE: not a function — `local` would error out here.
    pre_eod_h=$(TZ=America/New_York date +%H)
    pre_eod_m=$(TZ=America/New_York date +%M)
    pre_eod_dow=$(TZ=America/New_York date +%u)
    if [ "$pre_eod_dow" -le 5 ] \
        && [ $((10#$pre_eod_h * 60 + 10#$pre_eod_m)) -eq $((15 * 60 + 50)) ]; then
        run_health_check
    fi

    echo ""
    echo "[$timestamp] Sleeping 60 seconds (1 min)..."
    sleep 60
done
