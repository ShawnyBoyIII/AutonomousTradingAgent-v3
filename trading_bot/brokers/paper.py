"""
Paper Broker Adapter - V3.1 Task 1

Implements BrokerAdapter interface using existing PaperBroker.
Provides standardized interface for V3 while maintaining backward compatibility.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_bot.brokers.base import (
    BrokerAdapter,
    BrokerMode,
    BrokerAccount,
    BrokerPosition,
    BrokerOrder,
    OrderPreview,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.portfolio.ledger import PortfolioLedger
from trading_bot.config.settings import Settings


class PaperBrokerAdapter(BrokerAdapter):
    """
    Paper trading adapter implementing BrokerAdapter.
    
    Wraps existing PaperBroker to provide standardized interface.
    Always operates in PAPER mode with simulated fills.
    """
    
    def __init__(self, settings: Settings, ledger: PortfolioLedger | None = None):
        super().__init__(mode=BrokerMode.PAPER, config={})
        self.settings = settings
        self.ledger = ledger or PortfolioLedger(settings.app.state_db_path)
        self._paper_broker = PaperBroker(settings)
        self._connected = False
    
    def connect(self) -> bool:
        """Paper broker is always 'connected'."""
        self._connected = True
        return True
    
    def disconnect(self) -> None:
        """No-op for paper broker."""
        self._connected = False
    
    def is_authenticated(self) -> bool:
        """Paper broker requires no authentication."""
        return self._connected
    
    def get_account(self) -> BrokerAccount:
        """Fetch account from local ledger."""
        portfolio = self.ledger.load_portfolio()
        
        return BrokerAccount(
            account_id="PAPER_001",
            cash=Decimal(str(portfolio.cash)),
            equity=Decimal(str(portfolio.equity)),
            buying_power=Decimal(str(portfolio.cash)),  # Paper: cash = buying power
            currency="USD",
            timestamp=datetime.now(),
        )
    
    def get_positions(self) -> list[BrokerPosition]:
        """Fetch positions from local ledger."""
        portfolio = self.ledger.load_portfolio()
        positions = []
        
        for symbol, pos in portfolio.positions.items():
            # Get current price (if available)
            current_price = getattr(pos, 'current_price', None)
            if current_price is None:
                current_price = pos.avg_cost
            
            market_value = Decimal(str(current_price)) * Decimal(str(pos.quantity))
            cost_basis = Decimal(str(pos.avg_cost)) * Decimal(str(pos.quantity))
            unrealized_pnl = market_value - cost_basis
            
            positions.append(BrokerPosition(
                symbol=symbol,
                quantity=Decimal(str(pos.quantity)),
                avg_entry_price=Decimal(str(pos.avg_cost)),
                current_price=Decimal(str(current_price)) if current_price else None,
                market_value=market_value,
                unrealized_pnl=unrealized_pnl,
                timestamp=datetime.now(),
            ))
        
        return positions
    
    def get_orders(self, since: datetime | None = None) -> list[BrokerOrder]:
        """
        Fetch recent orders from decision log.
        
        Note: Paper broker doesn't have persistent order storage yet.
        Returns empty list for now - this would be enhanced with order history.
        """
        # TODO: Implement order history from decision log
        return []
    
    def get_order(self, order_id: str) -> BrokerOrder | None:
        """Fetch specific order."""
        # TODO: Implement order lookup
        return None
    
    def is_tradable(self, symbol: str) -> bool:
        """All symbols are tradable in paper mode."""
        # In reality, we'd check if we can fetch data for this symbol
        return True
    
    def get_quote(self, symbol: str) -> dict[str, Any]:
        """
        Get current quote for symbol.
        
        Uses yfinance provider via market_data module.
        """
        from trading_bot.data.market_data import fetch_bars
        
        try:
            bars = fetch_bars(symbol, period="1d", interval="1m")
            if bars.empty:
                return {"error": "No data available"}
            
            latest = bars.iloc[-1]
            return {
                "symbol": symbol,
                "bid": float(latest.get("bid", latest["close"])),
                "ask": float(latest.get("ask", latest["close"])),
                "last": float(latest["close"]),
                "volume": int(latest.get("volume", 0)),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            return {"error": str(e)}
    
    def preview_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> OrderPreview:
        """
        Preview what an order would look like.
        
        Safe to call - never executes.
        """
        # Get current quote
        quote = self.get_quote(symbol)
        
        if "error" in quote:
            return OrderPreview(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                estimated_price=Decimal("0"),
                estimated_total=Decimal("0"),
                estimated_fees=Decimal("0"),
                buying_power_impact=Decimal("0"),
                warnings=[f"Cannot get quote: {quote['error']}"],
                timestamp=datetime.now(),
            )
        
        # Estimate price
        if order_type == OrderType.MARKET:
            if side == OrderSide.BUY:
                estimated_price = Decimal(str(quote["ask"]))
            else:
                estimated_price = Decimal(str(quote["bid"]))
        else:
            estimated_price = price or Decimal(str(quote["last"]))
        
        # Calculate totals
        estimated_total = estimated_price * quantity
        
        # Paper broker fees
        fee_per_order = Decimal(str(self.settings.paper.fee_per_order))
        estimated_fees = fee_per_order
        
        # Buying power impact
        if side == OrderSide.BUY:
            buying_power_impact = estimated_total + estimated_fees
        else:
            buying_power_impact = -(estimated_total - estimated_fees)
        
        # Warnings
        warnings = []
        account = self.get_account()
        if side == OrderSide.BUY and buying_power_impact > account.buying_power:
            warnings.append(f"Insufficient buying power: need ${buying_power_impact}, have ${account.buying_power}")
        
        return OrderPreview(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            estimated_price=estimated_price,
            estimated_total=estimated_total,
            estimated_fees=estimated_fees,
            buying_power_impact=buying_power_impact,
            warnings=warnings,
            timestamp=datetime.now(),
        )
    
    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> BrokerOrder:
        """
        Submit an order to the paper broker.
        
        In PAPER mode, this simulates immediate fill.
        """
        from trading_bot.models.order import OrderRequest
        
        # Create order request
        order_request = OrderRequest(
            symbol=symbol,
            side=side.value,
            quantity=float(quantity),
            order_type=order_type.value,
            limit_price=float(price) if price else None,
        )
        
        # Execute via paper broker
        fill_result = self._paper_broker.submit_order(order_request)
        
        # Convert to BrokerOrder
        return BrokerOrder(
            order_id=str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            filled_quantity=quantity if fill_result.is_filled else Decimal("0"),
            status=OrderStatus.FILLED if fill_result.is_filled else OrderStatus.REJECTED,
            price=Decimal(str(fill_result.fill_price)) if fill_result.fill_price else None,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order.
        
        Paper broker fills immediately, so cancellation is usually not applicable.
        """
        # Paper orders fill immediately, so nothing to cancel
        return False
