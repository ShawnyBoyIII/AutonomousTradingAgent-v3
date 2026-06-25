from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from trading_bot.brokers.base import (
    BrokerAccount,
    BrokerMode,
    BrokerOrder,
    BrokerPosition,
    BrokerAdapter,
    OrderPreview,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_bot.config.settings import Settings


@dataclass(frozen=True)
class BrokerCapabilities:
    read_only: bool = True
    shadow_preview: bool = True
    live_submit: bool = False
    live_cancel: bool = False


@dataclass(frozen=True)
class BrokerConnectionStatus:
    connected: bool
    reason: str
    source: str
    freshness: str
    account_number: str | None
    synced_at: datetime | None
    capabilities: BrokerCapabilities


@dataclass(frozen=True)
class BrokerAccountSummary:
    account_number: str
    cash: float
    equity: float
    buying_power: float
    updated_at: datetime | None = None


@dataclass(frozen=True)
class BrokerPositionSummary:
    symbol: str
    quantity: float
    average_cost: float
    current_price: float | None = None
    market_value: float | None = None


@dataclass(frozen=True)
class BrokerOrderSummary:
    order_id: str
    symbol: str
    side: str
    quantity: float
    state: str
    order_type: str = "market"
    created_at: datetime | None = None


@dataclass(frozen=True)
class BrokerQuoteSnapshot:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None


@dataclass(frozen=True)
class BrokerIntentRecord:
    intent_id: str
    action: str
    mcp_action: str
    created_at: datetime
    account_number: str | None
    payload: dict[str, Any]
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class BrokerIntentResult:
    accepted: bool
    reason: str
    intent: BrokerIntentRecord


@dataclass(frozen=True)
class RobinhoodSyncMeta:
    source: str
    account_number: str | None
    synced_at: datetime | None
    fresh_until: datetime | None
    capabilities: BrokerCapabilities = field(default_factory=BrokerCapabilities)


class RobinhoodBrokerBoundary(BrokerAdapter):
    """MCP-backed Robinhood boundary.

    This boundary never authenticates with Robinhood directly and never executes
    MCP tool calls itself. It only consumes operator-synced snapshots and emits
    structured broker intents for later review/execution by Codex tooling.

    Subclass of :class:`BrokerAdapter`: the read-only / preview methods are
    implemented against MCP snapshot files. ``submit_order`` / ``cancel_order``
    raise ``NotImplementedError`` because live order execution is not supported
    locally; use :meth:`submit_live_order_intent` / :meth:`cancel_live_order_intent`
    to record intents for operator review.
    """

    def __init__(self, settings: Settings, now_fn: callable | None = None) -> None:
        # BrokerAdapter base init: MCP-only is always SHADOW locally.
        super().__init__(mode=BrokerMode.SHADOW, config={})
        self.settings = settings
        self._now_fn = now_fn or datetime.now
        self._state_dir = Path(settings.app.state_db_path).resolve().parent
        self._meta_path = self._state_dir / "robinhood_sync_meta.json"
        self._account_path = self._state_dir / "robinhood_account.json"
        self._positions_path = self._state_dir / "robinhood_positions.json"
        self._orders_path = self._state_dir / "robinhood_orders.json"
        self._quotes_path = self._state_dir / "robinhood_quotes.json"
        self._intent_log_path = self._state_dir / "robinhood_intents.jsonl"

    # ==================== MCP snapshot API ====================

    def get_status(self) -> BrokerConnectionStatus:
        if not self.settings.robinhood.enabled:
            return BrokerConnectionStatus(
                connected=False,
                reason="Robinhood integration disabled in config",
                source="disabled",
                freshness="disabled",
                account_number=None,
                synced_at=None,
                capabilities=BrokerCapabilities(
                    read_only=False,
                    shadow_preview=False,
                    live_submit=False,
                    live_cancel=False,
                ),
            )

        meta = self._load_meta()
        if meta is None:
            return BrokerConnectionStatus(
                connected=False,
                reason="No MCP sync snapshots found. Sync must be performed by Codex/operator using Robinhood MCP.",
                source="mcp",
                freshness="missing",
                account_number=None,
                synced_at=None,
                capabilities=BrokerCapabilities(),
            )

        missing_paths = [
            path.name
            for path in (self._account_path, self._positions_path, self._orders_path)
            if not path.exists()
        ]
        freshness = self._freshness(meta)
        if missing_paths:
            return BrokerConnectionStatus(
                connected=False,
                reason=f"Missing MCP broker snapshot(s): {', '.join(missing_paths)}",
                source=meta.source,
                freshness=freshness,
                account_number=meta.account_number,
                synced_at=meta.synced_at,
                capabilities=meta.capabilities,
            )

        return BrokerConnectionStatus(
            connected=freshness == "fresh",
            reason="MCP-backed broker snapshots available" if freshness == "fresh" else "MCP-backed broker snapshots are stale",
            source=meta.source,
            freshness=freshness,
            account_number=meta.account_number,
            synced_at=meta.synced_at,
            capabilities=meta.capabilities,
        )

    def get_accounts(self) -> list[BrokerAccountSummary]:
        account = self._load_account()
        return [account] if account is not None else []

    def get_portfolio(self, account_number: str) -> BrokerAccountSummary | None:
        account = self._load_account()
        if account is None or account.account_number != account_number:
            return None
        return account

    def get_positions_for(self, account_number: str) -> list[BrokerPositionSummary]:
        meta = self._load_meta()
        if meta is None or meta.account_number != account_number:
            return []
        payload = self._load_json(self._positions_path)
        if not isinstance(payload, list):
            return []
        positions: list[BrokerPositionSummary] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            try:
                positions.append(
                    BrokerPositionSummary(
                        symbol=str(row["symbol"]),
                        quantity=float(row["quantity"]),
                        average_cost=float(row["average_cost"]),
                        current_price=self._as_float(row.get("current_price")),
                        market_value=self._as_float(row.get("market_value")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return positions

    def get_orders_for(
        self,
        account_number: str,
        kind: str = "all",
        filters: dict[str, Any] | None = None,
    ) -> list[BrokerOrderSummary]:
        meta = self._load_meta()
        if meta is None or meta.account_number != account_number:
            return []
        payload = self._load_json(self._orders_path)
        if not isinstance(payload, list):
            return []
        filters = filters or {}
        orders: list[BrokerOrderSummary] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            if kind != "all" and row.get("kind", "equity") != kind:
                continue
            if filters.get("symbol") and row.get("symbol") != filters["symbol"]:
                continue
            try:
                orders.append(
                    BrokerOrderSummary(
                        order_id=str(row["order_id"]),
                        symbol=str(row["symbol"]),
                        side=str(row["side"]),
                        quantity=float(row["quantity"]),
                        state=str(row["state"]),
                        order_type=str(row.get("type", "market")),
                        created_at=self._parse_dt(row.get("created_at")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return orders

    def get_quotes(self, symbols: list[str]) -> list[BrokerQuoteSnapshot]:
        payload = self._load_json(self._quotes_path)
        if not isinstance(payload, list):
            return []
        wanted = {symbol.upper() for symbol in symbols}
        quotes: list[BrokerQuoteSnapshot] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in wanted:
                continue
            quotes.append(
                BrokerQuoteSnapshot(
                    symbol=symbol,
                    bid=self._as_float(row.get("bid")),
                    ask=self._as_float(row.get("ask")),
                    last=self._as_float(row.get("last")),
                )
            )
        return quotes

    def preview_shadow_order(
        self,
        *,
        account_number: str,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
    ) -> BrokerIntentResult:
        return self._record_intent(
            action="preview_shadow_order",
            mcp_action="review_equity_order",
            require_fresh=True,
            account_number=account_number,
            payload={
                "account_number": account_number,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "type": order_type,
                "limit_price": price,
            },
        )

    def submit_live_order_intent(
        self,
        *,
        account_number: str,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
    ) -> BrokerIntentResult:
        return self._record_intent(
            action="submit_live_order_intent",
            mcp_action="place_equity_order",
            require_fresh=True,
            account_number=account_number,
            payload={
                "account_number": account_number,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "type": order_type,
                "limit_price": price,
            },
        )

    def cancel_live_order_intent(
        self,
        *,
        account_number: str,
        order_id: str,
    ) -> BrokerIntentResult:
        return self._record_intent(
            action="cancel_live_order_intent",
            mcp_action="cancel_equity_order",
            require_fresh=True,
            account_number=account_number,
            payload={
                "account_number": account_number,
                "order_id": order_id,
            },
        )

    # ==================== BrokerAdapter interface ====================
    # Read-only / preview methods are backed by MCP snapshots. Live submit /
    # cancel are NOT supported locally - raise NotImplementedError; the
    # operator-mediated intent API above should be used instead.

    def is_authenticated(self) -> bool:
        return self.get_status().connected

    def get_account(self) -> BrokerAccount:
        status = self.get_status()
        account_number = status.account_number or ""
        summary = self.get_portfolio(account_number) if account_number else None
        if summary is None:
            raise RuntimeError("No fresh MCP account snapshot available")
        return BrokerAccount(
            account_id=summary.account_number,
            cash=Decimal(str(summary.cash)),
            equity=Decimal(str(summary.equity)),
            buying_power=Decimal(str(summary.buying_power)),
            timestamp=summary.updated_at,
        )

    def get_positions(self) -> list[BrokerPosition]:
        account_number = self._meta_account_number_or_raise()
        summaries = self.get_positions_for(account_number)
        return [
            BrokerPosition(
                symbol=s.symbol,
                quantity=Decimal(str(s.quantity)),
                avg_entry_price=Decimal(str(s.average_cost)),
                current_price=Decimal(str(s.current_price)) if s.current_price is not None else None,
                market_value=Decimal(str(s.market_value)) if s.market_value is not None else None,
                timestamp=self.get_status().synced_at,
            )
            for s in summaries
        ]

    def get_orders(self, since: datetime | None = None) -> list[BrokerOrder]:
        account_number = self._meta_account_number_or_raise()
        summaries = self.get_orders_for(account_number)
        orders: list[BrokerOrder] = []
        for s in summaries:
            if since is not None and s.created_at is not None and s.created_at < since:
                continue
            try:
                side = OrderSide(s.side.upper())
            except ValueError:
                side = OrderSide.BUY
            try:
                order_type = _normalize_order_type(s.order_type)
            except ValueError:
                order_type = OrderType.MARKET
            status_enum = _normalize_order_status(s.state)
            orders.append(
                BrokerOrder(
                    order_id=s.order_id,
                    symbol=s.symbol,
                    side=side,
                    order_type=order_type,
                    quantity=Decimal(str(s.quantity)),
                    filled_quantity=Decimal(0),
                    status=status_enum,
                    created_at=s.created_at,
                    updated_at=s.created_at,
                )
            )
        return orders

    def get_order(self, order_id: str) -> BrokerOrder | None:
        for order in self.get_orders():
            if order.order_id == order_id:
                return order
        return None

    def is_tradable(self, symbol: str) -> bool:
        if not self.is_authenticated():
            return False
        quotes = self.get_quotes([symbol])
        return any(q.symbol.upper() == symbol.upper() for q in quotes)

    def get_quote(self, symbol: str) -> dict[str, Any]:
        quotes = self.get_quotes([symbol])
        if not quotes:
            return {"symbol": symbol.upper(), "bid": None, "ask": None, "last": None}
        q = quotes[0]
        return {
            "symbol": q.symbol,
            "bid": q.bid,
            "ask": q.ask,
            "last": q.last,
        }

    def preview_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> OrderPreview:
        account_number = self._meta_account_number_or_raise()
        intent = self.preview_shadow_order(
            account_number=account_number,
            symbol=symbol,
            side=side.value,
            quantity=float(quantity),
            order_type=order_type.value,
            price=float(price) if price is not None else None,
        )
        if not intent.accepted:
            raise RuntimeError(intent.reason)
        quote = self.get_quote(symbol)
        estimated_price = Decimal(str(quote["last"])) if quote.get("last") is not None else (
            Decimal(str(price)) if price is not None else Decimal(0)
        )
        estimated_total = estimated_price * quantity
        return OrderPreview(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            estimated_price=estimated_price,
            estimated_total=estimated_total,
            estimated_fees=Decimal(0),
            buying_power_impact=estimated_total,
            warnings=[
                "Shadow preview only - MCP operator must review before live execution"
            ],
        )

    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> BrokerOrder:
        raise NotImplementedError(
            "Live order submission is not supported by RobinhoodBrokerBoundary. "
            "Use submit_live_order_intent() to record an MCP intent for operator review."
        )

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError(
            "Live order cancellation is not supported by RobinhoodBrokerBoundary. "
            "Use cancel_live_order_intent() to record an MCP intent for operator review."
        )

    def connect(self) -> bool:
        # MCP snapshots are file-based; there is no live connection to open.
        return self.is_authenticated()

    def disconnect(self) -> None:
        # No live connection to close.
        return None

    def _meta_account_number_or_raise(self) -> str:
        status = self.get_status()
        if not status.account_number:
            raise RuntimeError("No MCP broker snapshot available. Sync via Robinhood MCP first.")
        if not status.connected:
            raise RuntimeError(f"MCP broker snapshot is not fresh: {status.freshness}")
        return status.account_number

    def _record_intent(
        self,
        *,
        action: str,
        mcp_action: str,
        require_fresh: bool,
        account_number: str,
        payload: dict[str, Any],
    ) -> BrokerIntentResult:
        status = self.get_status()
        accepted = True
        reason = "Intent recorded for Codex/operator review"
        if not self.settings.robinhood.enabled:
            accepted = False
            reason = "Robinhood integration disabled in config"
        elif status.account_number and status.account_number != account_number:
            accepted = False
            reason = f"Snapshot account mismatch: expected {status.account_number}, got {account_number}"
        elif require_fresh and status.freshness != "fresh":
            accepted = False
            reason = (
                "Broker sync is stale or unavailable. Sync must be refreshed by Codex/operator using Robinhood MCP."
            )
        elif action == "submit_live_order_intent" and not status.capabilities.live_submit:
            accepted = False
            reason = "Live submit is not supported locally; operator must execute via Robinhood MCP."
        elif action == "cancel_live_order_intent" and not status.capabilities.live_cancel:
            accepted = False
            reason = "Live cancel is not supported locally; operator must execute via Robinhood MCP."

        created_at = self._now_fn()
        intent = BrokerIntentRecord(
            intent_id=f"{action}-{created_at.strftime('%Y%m%d%H%M%S%f')}",
            action=action,
            mcp_action=mcp_action,
            created_at=created_at,
            account_number=account_number,
            payload=payload,
            status="accepted" if accepted else "rejected",
            reason=None if accepted else reason,
        )
        self._append_intent(intent)
        return BrokerIntentResult(accepted=accepted, reason=reason, intent=intent)

    def _append_intent(self, intent: BrokerIntentRecord) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        with self._intent_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_intent_to_dict(intent)) + "\n")

    def _load_account(self) -> BrokerAccountSummary | None:
        payload = self._load_json(self._account_path)
        if not isinstance(payload, dict):
            return None
        account_number = payload.get("account_number") or self._load_meta_account_number()
        if not account_number:
            return None
        try:
            return BrokerAccountSummary(
                account_number=str(account_number),
                cash=float(payload["cash"]),
                equity=float(payload["equity"]),
                buying_power=float(payload["buying_power"]),
                updated_at=self._parse_dt(payload.get("updated_at")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _load_meta(self) -> RobinhoodSyncMeta | None:
        payload = self._load_json(self._meta_path)
        if not isinstance(payload, dict):
            return None
        caps_payload = payload.get("capabilities") or {}
        capabilities = BrokerCapabilities(
            read_only=bool(caps_payload.get("read_only", True)),
            shadow_preview=bool(caps_payload.get("shadow_preview", True)),
            live_submit=bool(caps_payload.get("live_submit", False)),
            live_cancel=bool(caps_payload.get("live_cancel", False)),
        )
        return RobinhoodSyncMeta(
            source=str(payload.get("source", "mcp")),
            account_number=str(payload["account_number"]) if payload.get("account_number") else None,
            synced_at=self._parse_dt(payload.get("synced_at")),
            fresh_until=self._parse_dt(payload.get("fresh_until")),
            capabilities=capabilities,
        )

    def _freshness(self, meta: RobinhoodSyncMeta) -> str:
        if meta.fresh_until is None:
            return "stale"
        return "fresh" if self._now_fn() <= meta.fresh_until else "stale"

    def _load_meta_account_number(self) -> str | None:
        meta = self._load_meta()
        return meta.account_number if meta else None

    def _load_json(self, path: Path) -> dict[str, Any] | list[Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _parse_dt(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def _as_float(self, value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

def _intent_to_dict(intent: BrokerIntentRecord) -> dict[str, Any]:
    payload = asdict(intent)
    payload["created_at"] = intent.created_at.isoformat()
    return payload


def _normalize_order_type(value: str) -> OrderType:
    normalized = str(value or "market").strip().lower()
    mapping = {
        "market": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "stop_market": OrderType.STOP,
        "stop": OrderType.STOP,
        "stop_limit": OrderType.STOP_LIMIT,
    }
    return mapping.get(normalized, OrderType.MARKET)


def _normalize_order_status(value: str) -> OrderStatus:
    normalized = str(value or "").strip().lower()
    mapping = {
        "queued": OrderStatus.PENDING,
        "new": OrderStatus.PENDING,
        "confirmed": OrderStatus.PENDING,
        "unconfirmed": OrderStatus.PENDING,
        "partially_filled": OrderStatus.PARTIAL,
        "partial": OrderStatus.PARTIAL,
        "filled": OrderStatus.FILLED,
        "cancelled": OrderStatus.CANCELLED,
        "canceled": OrderStatus.CANCELLED,
        "pending_cancelled": OrderStatus.CANCELLED,
        "rejected": OrderStatus.REJECTED,
        "failed": OrderStatus.REJECTED,
        "voided": OrderStatus.REJECTED,
    }
    return mapping.get(normalized, OrderStatus.PENDING)
