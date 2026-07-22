/* =============================================================
   Trading Bot Dashboard — client controller
   Vanilla ES2017+, no dependencies.
   ============================================================= */
(function () {
  "use strict";

  // -------------------------------------------------------------
  // State
  // -------------------------------------------------------------
  const STATE = {
    startingEquity: 350000,  // Updated 2026-07-08 after +$250K deposit
    history: [],            // ring buffer of equity samples (for sparkline)
    historyMax: 80,
    lastUpdate: null,
    sse: null,
    reconnectTimer: null,
    reconnectDelay: 2500,
    clockTimer: null,
  };

  // -------------------------------------------------------------
  // DOM helpers
  // -------------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  const escapeHTML = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  // -------------------------------------------------------------
  // Formatters
  // -------------------------------------------------------------
  const FMT_USD = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  const FMT_USD_SHORT = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 2,
  });

  const FMT_PCT = (n, digits = 2) =>
    (n >= 0 ? "+" : "") +
    (n * 100).toFixed(digits) +
    "%";

  const FMT_TIME = (ts) => {
    if (!ts) return "—";
    const d = ts instanceof Date ? ts : new Date(ts);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString([], { hour12: false });
  };

  const FMT_DATE = (d) =>
    d.toLocaleDateString([], { year: "numeric", month: "short", day: "2-digit" }).toUpperCase();

  const FMT_FULL_DATE = (d) =>
    d.toLocaleDateString([], { weekday: "short", year: "numeric", month: "short", day: "2-digit" });

  const FMT_RELATIVE = (ts) => {
    if (!ts) return "—";
    const d = ts instanceof Date ? ts : new Date(ts);
    if (isNaN(d.getTime())) return "—";
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60)   return Math.max(0, Math.floor(diff)) + "s ago";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    return Math.floor(diff / 86400) + "d ago";
  };

  const fmtUSD   = (n) => (n === null || n === undefined || isNaN(n)) ? "—" : FMT_USD.format(n);
  const fmtShort = (n) => (n === null || n === undefined || isNaN(n)) ? "—" : FMT_USD_SHORT.format(n);
  const fmtInt   = (n) => (n === null || n === undefined || isNaN(n)) ? "—" : new Intl.NumberFormat("en-US").format(Math.round(n));

  // -------------------------------------------------------------
  // Set a value with a brief flash on change
  // -------------------------------------------------------------
  function setValue(el, value, opts = {}) {
    if (!el) return;
    const prev = el.dataset.value;
    const next = value == null ? "" : String(value);
    if (opts.html) {
      if (prev !== next) {
        el.innerHTML = next;
        el.dataset.value = next;
        if (prev !== undefined) flash(el);
      }
    } else {
      if (el.textContent !== next) {
        el.textContent = next;
        el.dataset.value = next;
        if (prev !== undefined) flash(el);
      }
    }
  }

  function setAttr(el, name, value) {
    if (!el) return;
    if (el.getAttribute(name) !== String(value)) {
      el.setAttribute(name, value);
    }
  }

  function flash(el) {
    if (!el) return;
    el.classList.remove("flash");
    // force reflow to restart animation
    void el.offsetWidth;
    el.classList.add("flash");
  }

  // Inject flash styles once
  (function injectFlash() {
    if (document.getElementById("__flash_style__")) return;
    const style = document.createElement("style");
    style.id = "__flash_style__";
    style.textContent = `
      .flash { animation: flash-up 700ms cubic-bezier(0.22, 1, 0.36, 1); }
      @keyframes flash-up {
        0%   { background-color: rgba(255, 182, 39, 0.10); }
        100% { background-color: transparent; }
      }
      .num-pos { color: var(--pos); }
      .num-neg { color: var(--neg); }
      .num-mute { color: var(--ink-mute); }
    `;
    document.head.appendChild(style);
  })();

  // -------------------------------------------------------------
  // Portfolio render
  // -------------------------------------------------------------
  function renderPortfolio(data) {
    if (!data) return;

    const equity  = Number(data.equity) || 0;
    const cash    = Number(data.cash) || 0;
    // Prefer the API-supplied starting_equity over the HTML body attribute.
    // The body attribute is the static default; the API response reflects
    // the actual cohort boundary (graduation_since / equity_evaluation_since).
    if (Number.isFinite(Number(data.starting_equity)) && Number(data.starting_equity) > 0) {
      STATE.startingEquity = Number(data.starting_equity);
    }
    const positions = Array.isArray(data.positions) ? data.positions : [];
    const count   = positions.length;
    const totalUnrealized = Number(data.total_unrealized_pnl);
    const totalUnrealizedPct = Number(data.total_unrealized_pct);
    const winningPositions = Number(data.winning_positions);
    const losingPositions = Number(data.losing_positions);
    const totalCostBasis = positions.reduce((acc, p) => {
      const qty = Number(p.quantity) || 0;
      const avgCost = Number(p.avg_cost ?? p.average_cost);
      return Number.isFinite(avgCost) ? acc + (qty * avgCost) : acc;
    }, 0);

    // Record equity for sparkline
    recordEquity(equity);

    // Hero equity
    setValue($("equityValue"), fmtUSD(equity));
    $("equityValue").dataset.trend = equity >= STATE.startingEquity ? "up" : "down";

    // KPIs
    setValue($("cashValue"), fmtUSD(cash));
    setValue($("positionCount"), fmtInt(count));

    const exposure = equity > 0 ? (equity - cash) / equity : 0;
    const expEl = $("exposureValue");
    setValue(expEl, (exposure * 100).toFixed(1) + "%");
    expEl.classList.toggle("kpi__value--pos", exposure > 0.5);
    expEl.classList.toggle("kpi__value--neg", exposure < 0.1);

    const liveExposureEl = $("liveExposureValue");
    if (liveExposureEl) {
      setValue(liveExposureEl, (exposure * 100).toFixed(1) + "%");
      setAttr(liveExposureEl, "data-trend", exposure > 0.5 ? "up" : exposure < 0.1 ? "down" : "flat");
    }

    const openPnlEl = $("openPnlValue");
    if (openPnlEl) {
      const liveUnrealized = Number.isFinite(totalUnrealized)
        ? totalUnrealized
        : positions.reduce((acc, p) => acc + (Number(p.unrealized_pnl) || 0), 0);
      setValue(openPnlEl, fmtUSD(liveUnrealized));
      setAttr(openPnlEl, "data-trend", liveUnrealized > 0 ? "up" : liveUnrealized < 0 ? "down" : "flat");
    }

    const openPnlPctEl = $("openPnlPct");
    if (openPnlPctEl) {
      const liveUnrealizedPct = Number.isFinite(totalUnrealizedPct)
        ? totalUnrealizedPct
        : Number.isFinite(totalUnrealized) && totalCostBasis > 0
          ? totalUnrealized / totalCostBasis
          : null;
      setValue(openPnlPctEl, liveUnrealizedPct === null ? "—" : FMT_PCT(liveUnrealizedPct, 2));
      setAttr(openPnlPctEl, "data-trend", liveUnrealizedPct === null ? "flat" : liveUnrealizedPct > 0 ? "up" : liveUnrealizedPct < 0 ? "down" : "flat");
    }

    setValue($("liveWinnersValue"), Number.isFinite(winningPositions) ? fmtInt(winningPositions) : fmtInt(0));
    setValue($("liveLosersValue"), Number.isFinite(losingPositions) ? fmtInt(losingPositions) : fmtInt(0));

    // Unrealized P&L (sum)
    const totalUnreal = positions.reduce((acc, p) => acc + (Number(p.unrealized_pnl) || 0), 0);
    renderPnL(totalUnreal, equity);
    void totalUnreal;

    // Hero gauge for day change vs starting equity
    renderGauge(equity);

    // Positions table
    renderPositions(positions);

    // Engine state + telemetry
    renderTelemetry({ portfolio: data });

    // Portfolio "active" badge — reflected on positions card now
    const posBadge = $("positionsBadge");
    const posBadgeNum = $("positionsBadgeNum");
    if (posBadgeNum) {
      setValue(posBadgeNum, String(count));
    }
    if (posBadge) {
      posBadge.dataset.state = count > 0 ? "ok" : "";
    }
  }

  // -------------------------------------------------------------
  // P&L
  // -------------------------------------------------------------
  function renderPnL(unreal, equity) {
    // P&L = current equity - starting equity (equity already includes unrealized)
    void unreal;
    const pnl = (equity || 0) - STATE.startingEquity;
    const pct = STATE.startingEquity > 0 ? pnl / STATE.startingEquity : 0;
    const trend = pnl >= 0 ? "up" : "down";

    const pnlEl = $("pnlValue");
    setValue(pnlEl, fmtUSD(Math.abs(pnl)));
    setAttr(pnlEl, "data-trend", trend);

    const pctEl = $("pnlPct");
    setValue(pctEl, FMT_PCT(pct, 2));
    setAttr(pctEl, "data-trend", trend);
  }

  // -------------------------------------------------------------
  // Day-change gauge
  // -------------------------------------------------------------
  function renderGauge(equity) {
    const needle = $("gaugeNeedle");
    const track  = $("gaugeTrack");
    if (!needle || !track) return;

    const pct = STATE.startingEquity > 0
      ? (equity - STATE.startingEquity) / STATE.startingEquity
      : 0;

    // Map [-10%, +10%] to [0%, 100%]
    const range = 0.10;
    const clamped = Math.max(-range, Math.min(range, pct));
    const pos = 50 + (clamped / range) * 50;
    needle.style.left = pos.toFixed(2) + "%";
    needle.dataset.trend = pct >= 0 ? "up" : "down";

    setValue($("gaugeMid"), "0%");
    setValue($("gaugeMax"), pct >= 0 ? "+10%" : "−10%");
  }

  // -------------------------------------------------------------
  // Sparkline (rolling equity history)
  // -------------------------------------------------------------
  function recordEquity(value) {
    if (typeof value !== "number" || isNaN(value)) return;
    const last = STATE.history[STATE.history.length - 1];
    // Avoid storing identical consecutive values (keeps line readable)
    if (last === value) return;
    STATE.history.push(value);
    if (STATE.history.length > STATE.historyMax) STATE.history.shift();
    drawSparkline();
  }

  function drawSparkline() {
    const wrap = $("sparklineWrap");
    const line = $("sparkline");
    const fill = $("sparklineFill");
    if (!wrap || !line || !fill) return;
    const data = STATE.history;
    if (data.length < 2) {
      line.removeAttribute("points");
      fill.removeAttribute("points");
      return;
    }

    const w = 200, h = 56;
    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;
    const stepX = w / (STATE.historyMax - 1);

    const pts = data.map((v, i) => {
      const x = i * stepX;
      const y = h - 4 - ((v - min) / range) * (h - 8);
      return [x.toFixed(2), y.toFixed(2)];
    });

    const linePts = pts.map((p) => p.join(",")).join(" ");
    line.setAttribute("points", linePts);

    const fillPts =
      linePts +
      ` ${(pts[pts.length - 1][0])},${h} ` +
      `${pts[0][0]},${h}`;
    fill.setAttribute("points", fillPts);

    const trend = data[data.length - 1] >= data[0] ? "up" : "down";
    wrap.setAttribute("data-trend", data[0] === data[data.length - 1] ? "flat" : trend);
  }

  // -------------------------------------------------------------
  // Positions
  // -------------------------------------------------------------
  function renderPositions(positions) {
    const container = $("positionsList");
    const badge = $("positionsBadge");
    if (!container) return;

    setValue(badge, String(positions.length));
    const tabCount = $("openTabCount");
    if (tabCount) tabCount.textContent = String(positions.length);

    if (!positions.length) {
      container.innerHTML = '<div class="empty">No open positions</div>';
      return;
    }

    // Compute total unrealized for scaling per-row bars
    const totalAbs = positions.reduce((acc, p) => acc + Math.abs(Number(p.unrealized_pnl) || 0), 0) || 1;

    const rows = positions.map((p) => {
      const qty      = Number(p.quantity) || 0;
      const avg      = Number(p.avg_cost ?? p.average_cost) || 0;
      const price    = Number(p.current_price ?? avg) || 0;
      const mv       = Number(p.market_value ?? qty * price) || qty * price;
      const pnl      = Number(p.unrealized_pnl ?? (price - avg) * qty) || 0;
      const unrealPct = Number(p.unrealized_pct);
      const pct      = Math.abs(pnl) / totalAbs;
      const trend    = pnl > 0 ? "pnl-pos" : (pnl < 0 ? "pnl-neg" : "pnl-flat");
      const markLive = p.mark_is_live === true;
      const staleCls = markLive ? "" : " position-row--stale";
      const staleTitle = markLive ? "" : ' title="Live mark unavailable — showing avg cost"';
      const staleTag = markLive ? "" : ' <span class="mark-stale" aria-label="Live mark unavailable">~</span>';
      const sym      = escapeHTML(p.symbol || p.ticker || "—");
      return `
        <tr class="position-row ${trend}${staleCls}"${staleTitle}>
          <td class="sym">${sym}${staleTag}</td>
          <td class="num qty">${fmtInt(qty)}</td>
          <td class="num">${fmtUSD(avg)}</td>
          <td class="num mark">${fmtUSD(price)}</td>
          <td class="num">${fmtUSD(mv)}</td>
          <td class="num ${trend}">
            ${fmtUSD(pnl)}
            <span class="pnl-bar"><i style="transform:scaleX(${pct.toFixed(3)})"></i></span>
          </td>
          <td class="num ${trend}">${Number.isFinite(unrealPct) ? FMT_PCT(unrealPct, 2) : "—"}</td>
        </tr>
      `;
    }).join("");

    container.innerHTML = `
      <div class="positions-wrap">
        <table class="positions-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th class="num">Qty</th>
              <th class="num">Avg Cost</th>
              <th class="num">Mark</th>
              <th class="num">Market Value</th>
              <th class="num">Unrealized P&L</th>
              <th class="num">Unrealized %</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;

    // Animate pnl bars in after the DOM settles
    requestAnimationFrame(() => {
      container.querySelectorAll(".pnl-bar > i").forEach((el) => el.classList.add("on"));
    });
  }

  // -------------------------------------------------------------
  // Closed Trades (ledger leaf)
  // -------------------------------------------------------------
  // Settlement stamps drive an opposite-to-color logic:
  //   EOD/THESIS/TIME/MANUAL = neutral ink-mute
  //   STOP  = vermilion (neg)
  //   TARGET = verdant (pos)
  // Regime badges classify by exit regime (or market regime at entry
  // if exit regime absent).
  function stampReason(reason) {
    const r = String(reason || "").toLowerCase().trim();
    if (!r)            return '<span class="stamp">—</span>';
    if (r === "stop")  return `<span class="stamp stamp--stop">${escapeHTML(r)}</span>`;
    if (r === "target")return `<span class="stamp stamp--target">${escapeHTML(r)}</span>`;
    if (r === "thesis" || r === "thesis_exit") {
                        return `<span class="stamp stamp--thesis">${escapeHTML("thesis")}</span>`;
    }
    return `<span class="stamp stamp--eod">${escapeHTML(r)}</span>`;
  }

  function regimeBadge(regime) {
    const r = String(regime || "").toLowerCase().trim();
    if (!r) return '<span class="regime regime--unknown">—</span>';
    const label = r.replace(/_/g, " ").toUpperCase();
    return `<span class="regime regime--${escapeHTML(r)}">${escapeHTML(label)}</span>`;
  }

  function renderClosedTrades(payload) {
    const container = $("closedTradesList");
    const badge     = $("closedBadge");
    const badgeNum  = $("closedBadgeNum");
    if (!container) return;

    const trades = (payload && payload.trades) || [];
    if (badgeNum) badgeNum.textContent = String(trades.length);
    const tabCount  = $("closedTabCount");
    if (tabCount) tabCount.textContent = String(trades.length);

    if (badge) {
      setAttr(badge, "data-state",
        payload && payload.error ? "critical"
        : trades.length ? "ok"
        : "ok"
      );
    }

    if (!trades.length) {
      container.innerHTML =
        '<div class="empty">No closed trades yet</div>';
      return;
    }

    // Cap initial render to keep the leaf readable; allow expansion via "show all".
    const INITIAL_LIMIT = 6;
    const visible = trades.slice(0, INITIAL_LIMIT);
    const overflow = Math.max(0, trades.length - visible.length);

    const rows = visible.map(buildClosedTradesRow).join("");

    const overflowFooter = overflow > 0
      ? `<button type="button" class="closed-expand" id="closedExpandBtn">
           <span class="closed-expand__icon" aria-hidden="true">+</span>
           <span class="closed-expand__text">Show ${overflow} more</span>
         </button>`
      : "";

    container.innerHTML = `
      <div class="closed-wrap">
        <table class="closed-table">
          <caption>Lifecycle of closed round-trip trades, ordered by exit time, newest first</caption>
          <thead>
            <tr>
              <th scope="col">Ticker</th>
              <th scope="col">Entry → Exit</th>
              <th scope="col" class="num">P&amp;L</th>
              <th scope="col">Hold</th>
              <th scope="col">Reason</th>
              <th scope="col">Regime</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
        ${overflowFooter}
      </div>
    `;

    requestAnimationFrame(() => {
      container.querySelectorAll(".hold__bar > i").forEach((el) =>
        el.parentElement.classList.add("on")
      );
    });

    // Wire the expand button: render the full set of rows.
    const expandBtn = $("closedExpandBtn");
    if (expandBtn) {
      expandBtn.addEventListener("click", () => {
        const allRows = trades.map(buildClosedTradesRow).join("");
        const tbody = container.querySelector("tbody");
        tbody.innerHTML = allRows;
        expandBtn.remove();
        requestAnimationFrame(() => {
          container.querySelectorAll(".hold__bar > i").forEach((el) =>
            el.parentElement.classList.add("on")
          );
        });
      });
    }
  }

  // Build a single closed-trades row HTML string from a trade record.
  // Exposed as a top-level helper so the expand-button can reuse it
  // without calling renderClosedTrades recursively.
  function buildClosedTradesRow(t) {
    const sym      = escapeHTML(t.ticker || "—");
    const pnl      = Number(t.pnl || 0);
    const ep       = Number(t.entry_price || 0);
    const xp       = Number(t.exit_price || 0);
    const minutes  = Number(t.hold_duration_minutes || 0);
    const trend    = pnl > 0 ? "pnl-pos" : (pnl < 0 ? "pnl-neg" : "pnl-flat");
    const exitTrend = trend;

    // Trading-day cap for the hold-duration bar (390 min ≈ 6.5 hr session)
    const TRADE_DAY_MIN = 390;
    const widthPct = Math.min(100, (minutes / TRADE_DAY_MIN) * 100).toFixed(1);

    const stamp  = stampReason(t.exit_reason);
    const regime = regimeBadge(t.exit_regime || t.market_regime);

    const roundtrip = `
      <span class="roundtrip">
        <span class="roundtrip__entry">${fmtUSD(ep)}</span>
        <span class="roundtrip__arrow" aria-hidden="true">→</span>
        <span class="roundtrip__exit ${exitTrend}">${fmtUSD(xp)}</span>
      </span>
    `;

    const hold = minutes > 0 ? `
      <span class="hold">
        <span class="hold__bar" aria-hidden="true"><i style="width:${widthPct}%"></i></span>
        <span class="hold__min">${Math.round(minutes)}m</span>
      </span>
    ` : '<span class="hold__min hold__min--off">—</span>';

    return `
      <tr>
        <td class="sym">${sym}</td>
        <td>${roundtrip}</td>
        <td class="num ${trend}">${fmtUSD(pnl)}</td>
        <td>${hold}</td>
        <td>${stamp}</td>
        <td>${regime}</td>
      </tr>
    `;
  }

  // -------------------------------------------------------------
  // Alerts
  // -------------------------------------------------------------
  function renderAlerts(data) {
    const container = $("alertsList");
    const badge     = $("alertBadge");
    if (!container) return;

    const alerts = (data && data.alerts) || [];
    setValue(badge, String(alerts.length));
    const state = data && data.has_critical ? "critical" : (alerts.length ? "warning" : "ok");
    setAttr(badge, "data-state", state);

    if (!alerts.length) {
      container.innerHTML = '<div class="empty">All systems nominal</div>';
      return;
    }

    container.innerHTML = alerts.map((a) => {
      const lvl = a.level === "critical" ? "critical" : "warning";
      return `
        <div class="alert alert--${lvl}" role="status">
          <div class="alert__body">
            <span class="alert__level">${escapeHTML(lvl)} · ${escapeHTML(a.category || "system")}</span>
            <span class="alert__message">${escapeHTML(a.message)}</span>
          </div>
          <span class="alert__time">${escapeHTML(FMT_RELATIVE(a.timestamp))}</span>
        </div>
      `;
    }).join("");
  }

  // -------------------------------------------------------------
  // Health
  // -------------------------------------------------------------
  function renderHealth(data) {
    const container = $("healthList");
    const badge     = $("healthBadge");
    if (!container) return;

    const checks = (data && data.checks) || [];
    const status = (data && data.status) || "ok";
    setValue(badge, status.toUpperCase());
    setAttr(badge, "data-state", status);

    if (!checks.length) {
      container.innerHTML = '<div class="empty">No checks reported</div>';
      return;
    }

    container.innerHTML = checks.map((c) => {
      const st = c.status || "ok";
      return `
        <div class="health-row">
          <span class="health-row__name">${escapeHTML(c.name)}</span>
          <span class="health-row__status" data-state="${escapeHTML(st)}">${escapeHTML(st)}</span>
          ${c.message ? `<span class="health-row__msg">${escapeHTML(c.message)}</span>` : ""}
        </div>
      `;
    }).join("");

    renderKillSwitch(data && data.kill_switch);
    renderTelemetry({ health: data });
  }

  // -------------------------------------------------------------
  // Kill Switch
  // -------------------------------------------------------------
  function renderKillSwitch(ks) {
    if (!ks) return;
    const panel   = $("killPanel");
    const stateEl = $("killStateValue");
    const detail  = $("killStateDetail");
    const lever   = $("emergencyBtn");
    const leverLbl= $("emergencyLabel");
    const badge   = $("killBadge");

    const halted = !!ks.active;
    setAttr(panel, "data-state", halted ? "halted" : "armed");
    setAttr(stateEl, "data-state", halted ? "halted" : "armed");
    setValue(stateEl, halted ? "Halted" : "Armed");
    setValue(detail, halted
      ? `Reason: ${ks.reason || "manual"}`
      : "Standing by · ready to halt"
    );

    if (lever) {
      lever.disabled = halted;
      setValue(leverLbl, halted ? "Halted" : "Pull to halt");
    }

    if (badge) {
      setValue(badge, halted ? "Halted" : "Armed");
      setAttr(badge, "data-state", halted ? "critical" : "ok");
    }

    // Mirror onto mode rail engine indicator
    const eng = $("engineState");
    if (eng) setValue(eng, halted ? "Halted" : "Standby");
  }

  // -------------------------------------------------------------
  // Telemetry
  // -------------------------------------------------------------
  function renderTelemetry(partial) {
    const lastEl = $("telemetryLastFill");
    const freshEl= $("telemetryDataFresh");
    const heartbeatEl = $("telemetryHeartbeat");
    const regimeEl = $("telemetryRegime");

    if (partial && partial.portfolio) {
      setValue(freshEl, FMT_TIME(partial.portfolio.timestamp));
      STATE.lastUpdate = partial.portfolio.timestamp;
    }
    if (lastEl) {
      // last fill is computed from trades list; updated by renderTrades()
    }
    if (regimeEl) {
      // Heuristic from portfolio state — placeholder; backend doesn't expose regime here.
      const cash = partial && partial.portfolio ? Number(partial.portfolio.cash) || 0 : null;
      const eq   = partial && partial.portfolio ? Number(partial.portfolio.equity) || 0 : null;
      if (cash !== null && eq) {
        const exp = (eq - cash) / eq;
        const regime = exp > 0.6 ? "Risk-On" : (exp < 0.1 ? "Defensive" : "Balanced");
        setValue(regimeEl, regime);
      }
    }
    if (heartbeatEl && STATE.lastUpdate) {
      tickHeartbeat();
    }
  }

  function tickHeartbeat() {
    if (!STATE.lastUpdate) return;
    const elapsed = Math.max(0, Math.floor((Date.now() - new Date(STATE.lastUpdate).getTime()) / 1000));
    setValue($("telemetryHeartbeat"), elapsed + " s ago");
  }

  // -------------------------------------------------------------
  // Trades
  // -------------------------------------------------------------
  function renderTrades(data) {
    const container = $("tradesList");
    if (!container) return;
    const trades = (data && data.trades) || [];

    // Update today's count in KPI
    const todayEl = $("tradesTodayValue");
    if (todayEl) {
      const today = new Date().toDateString();
      const count = trades.filter((t) => {
        if (!t.timestamp) return false;
        try { return new Date(t.timestamp).toDateString() === today; } catch { return false; }
      }).length;
      setValue(todayEl, String(count));
    }

    // Update last fill telemetry
    if (trades.length) {
      const last = trades[0];
      const when = FMT_RELATIVE(last.timestamp);
      const side = last.side || "?";
      const sym  = last.symbol || "—";
      const qty  = fmtInt(last.quantity);
      const px   = fmtUSD(last.price);
      setValue($("telemetryLastFill"), `${side} ${sym} ${qty}@${px} · ${when}`);
    }

    if (!trades.length) {
      container.innerHTML = '<div class="empty">No recent trades</div>';
      return;
    }

    container.innerHTML = `
      <div class="trades-grid">
        ${trades.map((t) => {
          const side = (t.side || "?").toLowerCase();
          const sideLbl = (t.side || "?").toUpperCase();
          return `
            <div class="trade">
              <span class="trade__side trade__side--${escapeHTML(side)}">${escapeHTML(sideLbl)}</span>
              <div class="trade__body">
                <span class="trade__symbol">${escapeHTML(t.symbol || "—")}</span>
                <span class="trade__detail">${fmtInt(t.quantity)} @ ${fmtUSD(t.price)}</span>
              </div>
              <span class="trade__time">${escapeHTML(FMT_TIME(t.timestamp))}</span>
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  // -------------------------------------------------------------
  // Clock + heartbeat tickers
  // -------------------------------------------------------------
  function startClock() {
    const clock = $("clock");
    const date  = $("dateLabel");
    const footer= $("footerTime");
    if (!clock || !date) return;

    const tick = () => {
      const now = new Date();
      setValue(clock, now.toLocaleTimeString([], { hour12: false }));
      setValue(date,  FMT_DATE(now));
      if (footer) setValue(footer, FMT_FULL_DATE(now) + " · " + now.toLocaleTimeString([], { hour12: false }));
      tickHeartbeat();
    };
    tick();
    STATE.clockTimer = setInterval(tick, 1000);
  }

  // -------------------------------------------------------------
  // SSE + REST
  // -------------------------------------------------------------
  function setConnection(state) {
    const el = $("connectionStatus");
    const txt = $("connectionText");
    if (!el) return;
    setAttr(el, "data-state", state);
    if (txt) {
      setValue(txt, state === "connected" ? "Live"
        : state === "connecting" ? "Syncing"
        : "Offline");
    }
  }

  function connect() {
    if (STATE.sse) {
      try { STATE.sse.close(); } catch (_) {}
    }
    setConnection("connecting");
    let es;
    try {
      es = new EventSource("/api/stream");
    } catch (e) {
      console.warn("EventSource unavailable, falling back to polling", e);
      pollLoop();
      return;
    }
    STATE.sse = es;
    es.onopen = () => setConnection("connected");
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        if (payload && payload.error) {
          console.warn("stream error:", payload.error);
          return;
        }
        applyUpdate(payload);
      } catch (e) {
        console.error("parse error:", e);
      }
    };
    es.onerror = () => {
      setConnection("disconnected");
      try { es.close(); } catch (_) {}
      if (STATE.reconnectTimer) clearTimeout(STATE.reconnectTimer);
      STATE.reconnectTimer = setTimeout(connect, STATE.reconnectDelay);
    };
  }

  // Polling fallback for browsers without SSE (or restricted environments)
  function pollLoop() {
    let stop = false;
    const run = async () => {
      try {
        const [portfolio, health, alerts, trades, closed] = await Promise.all([
          fetchJSON("/api/portfolio"),
          fetchJSON("/api/health"),
          fetchJSON("/api/alerts"),
          fetchJSON("/api/trades"),
          fetchJSON("/api/closed-trades"),
        ]);
        if (stop) return;
        if (portfolio) renderPortfolio(portfolio);
        if (health) renderHealth(health);
        if (alerts) renderAlerts(alerts);
        if (trades) renderTrades(trades);
        if (closed) renderClosedTrades(closed);
        setConnection("connected");
      } catch (e) {
        setConnection("disconnected");
      }
    };
    run();
    const id = setInterval(run, 5000);
    STATE.pollTimer = id;
  }

  async function fetchJSON(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  function applyUpdate(payload) {
    if (!payload) return;
    if (payload.portfolio) renderPortfolio(payload.portfolio);
    if (payload.health)    renderHealth(payload.health);
    if (payload.alerts)    renderAlerts(payload.alerts);
    if (payload.trades)    renderTrades(payload.trades);
    if (payload.closed_trades) renderClosedTrades(payload.closed_trades);
  }

  async function bootstrap() {
    try {
      const [portfolio, health, alerts, trades, closed] = await Promise.all([
        fetchJSON("/api/portfolio").catch(() => null),
        fetchJSON("/api/health").catch(() => null),
        fetchJSON("/api/alerts").catch(() => null),
        fetchJSON("/api/trades").catch(() => null),
        fetchJSON("/api/closed-trades").catch(() => null),
      ]);
      if (portfolio) renderPortfolio(portfolio);
      if (health)    renderHealth(health);
      if (alerts)    renderAlerts(alerts);
      if (trades)    renderTrades(trades);
      if (closed)    renderClosedTrades(closed);
    } catch (e) {
      console.warn("bootstrap fetch failed", e);
    }
    startClock();
    connect();
    bindBookTabs();
  }

  // -------------------------------------------------------------
  // Book tabs — switch between Open and Closed views
  // -------------------------------------------------------------
  // Persists the active tab in localStorage so refreshes feel sticky.
  const BOOK_TAB_STORAGE = "book-active-tab";

  function getActiveBookTab() {
    try {
      const t = window.localStorage.getItem(BOOK_TAB_STORAGE);
      return t === "closed" ? "closed" : "open";
    } catch (_) {
      return "open";
    }
  }

  function setActiveBookTab(name) {
    try { window.localStorage.setItem(BOOK_TAB_STORAGE, name); } catch (_) { /* ignore */ }
  }

  function activateBookTab(name) {
    const tabs    = ["open", "closed"];
    const target = tabs.includes(name) ? name : "open";
    tabs.forEach((t) => {
      const tabBtn = $(`${t}Tab`);
      const pane   = $(`${t}Pane`);
      if (!tabBtn || !pane) return;
      const isActive = (t === target);
      tabBtn.classList.toggle("is-active", isActive);
      tabBtn.setAttribute("aria-selected", isActive ? "true" : "false");
      tabBtn.setAttribute("tabindex", isActive ? "0" : "-1");
      pane.classList.toggle("is-active", isActive);
      if (isActive) {
        pane.removeAttribute("hidden");
      } else {
        pane.setAttribute("hidden", "");
      }
    });
    setActiveBookTab(target);
  }

  function bindBookTabs() {
    const open   = $("openTab");
    const closed = $("closedTab");
    if (!open || !closed) return;

    // Activate default — "open" unless user previously chose "closed".
    const desired = getActiveBookTab();
    activateBookTab(desired);

    const handler = (name) => () => activateBookTab(name);
    open.addEventListener("click", handler("open"));
    closed.addEventListener("click", handler("closed"));

    // Keyboard: left/right arrows cycle, Home/End jump
    const order = ["open", "closed"];
    [open, closed].forEach((btn, i) => {
      btn.addEventListener("keydown", (ev) => {
        if (ev.key === "ArrowRight") {
          ev.preventDefault();
          (i === order.length - 1 ? $(`${order[0]}Tab`) : $(`${order[i+1]}Tab`)).focus();
          activateBookTab(order[(i + 1) % order.length]);
        } else if (ev.key === "ArrowLeft") {
          ev.preventDefault();
          (i === 0 ? $(`${order[order.length - 1]}Tab`) : $(`${order[i-1]}Tab`)).focus();
          activateBookTab(order[(i - 1 + order.length) % order.length]);
        } else if (ev.key === "Home") {
          ev.preventDefault();
          activateBookTab(order[0]);
          $(`${order[0]}Tab`).focus();
        } else if (ev.key === "End") {
          ev.preventDefault();
          activateBookTab(order[order.length - 1]);
          $(`${order[order.length - 1]}Tab`).focus();
        }
      });
    });
  }

  // -------------------------------------------------------------
  // Kill-switch action
  // -------------------------------------------------------------
  function bindKillSwitch() {
    const lever = $("emergencyBtn");
    if (!lever) return;
    lever.addEventListener("click", async () => {
      if (lever.disabled) return;
      const ok = window.confirm(
        "Halt all trading?\n\nThis will engage the kill switch and reject all new orders until resumed."
      );
      if (!ok) return;
      try {
        lever.disabled = true;
        const r = await fetch("/api/kill-switch/halt", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "Dashboard emergency stop" }),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        // Optimistic update; SSE will reconcile.
        const ks = { active: true, reason: "Dashboard emergency stop", since: new Date().toISOString() };
        renderKillSwitch(ks);
      } catch (e) {
        console.error("halt failed", e);
        lever.disabled = false;
        window.alert("Failed to halt. Check the console for details.");
      }
    });
  }

  // -------------------------------------------------------------
  // Init
  // -------------------------------------------------------------
  function init() {
    const body = document.body;
    const startAttr = body && body.dataset ? body.dataset.startingEquity : null;
    const start = parseFloat(startAttr);
    if (!isNaN(start) && start > 0) STATE.startingEquity = start;

    // Static hero defaults so first paint is not empty
    setValue($("cashValue"), "—");
    setValue($("equityValue"), "—");
    setValue($("pnlValue"), "—");
    setValue($("pnlPct"), "—");
    setValue($("positionCount"), "—");
    setValue($("exposureValue"), "—");
    setValue($("openPnlValue"), "—");
    setValue($("openPnlPct"), "—");
    setValue($("liveWinnersValue"), "—");
    setValue($("liveLosersValue"), "—");
    setValue($("liveExposureValue"), "—");
    setValue($("tradesTodayValue"), "—");
    setValue($("gaugeMid"), "0%");
    setValue($("gaugeMax"), "±10%");

    bindKillSwitch();
    bootstrap();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
