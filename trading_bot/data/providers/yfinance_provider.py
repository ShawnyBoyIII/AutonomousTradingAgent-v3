from __future__ import annotations


class YFinanceProvider:
    def fetch_bars(self, symbol: str, period: str, interval: str):
        import yfinance as yf
        from trading_bot.data.market_data import normalize_ohlcv_frame

        ticker = yf.Ticker(symbol)
        frame = ticker.history(period=period, interval=interval, auto_adjust=False)
        if frame.empty:
            raise ValueError(f"No market data returned for {symbol}")
        return normalize_ohlcv_frame(frame)
