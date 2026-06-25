from __future__ import annotations


class YFinanceProvider:
    def fetch_bars(
        self,
        symbol: str,
        period: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
    ):
        import yfinance as yf
        from trading_bot.data.market_data import normalize_ohlcv_frame

        ticker = yf.Ticker(symbol)
        # Use start/end if provided, otherwise fall back to period
        if start is not None or end is not None:
            frame = ticker.history(
                start=start, end=end, interval=interval, auto_adjust=False
            )
        else:
            frame = ticker.history(period=period, interval=interval, auto_adjust=False)
        if frame.empty:
            raise ValueError(f"No market data returned for {symbol}")
        return normalize_ohlcv_frame(frame)

    def fetch_small_cap_candidates(
        self,
        limit: int = 200,
        screeners: list[str] | None = None,
    ):
        import yfinance as yf

        rows: list[dict[str, object]] = []
        for source in screeners or ["aggressive_small_caps", "small_cap_gainers"]:
            result = yf.screen(source, count=min(limit, 250))
            quotes = result.get("quotes", []) if isinstance(result, dict) else []
            for quote in quotes:
                if not isinstance(quote, dict):
                    continue
                symbol = str(quote.get("symbol", "")).upper().strip()
                if not symbol:
                    continue
                rows.append({**quote, "source": source})
        return rows
