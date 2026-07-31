from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def add_ema(frame: "pd.DataFrame", period: int, column_name: str) -> "pd.DataFrame":
    prices = _close_prices(frame)
    values = _ema_values(prices, period)
    return _with_column(frame, column_name, values)


def add_rsi(frame: "pd.DataFrame", period: int = 14) -> "pd.DataFrame":
    prices = _close_prices(frame)
    values = _rsi_values(prices, period)
    return _with_column(frame, f"rsi_{period}", values)


def add_sma(frame: "pd.DataFrame", period: int = 14, column_name: str | None = None) -> "pd.DataFrame":
    prices = _close_prices(frame)
    values = _sma_values(prices, period)
    return _with_column(frame, column_name or f"sma_{period}", values)


def add_atr(frame: "pd.DataFrame", period: int = 14, column_name: str | None = None) -> "pd.DataFrame":
    required_columns = {"high", "low", "close"}
    if not required_columns.issubset(frame.columns):
        raise KeyError("missing required price columns: high, low, close")
    _validate_period(period)
    highs = frame["high"].to_numpy(dtype=float).tolist()
    lows = frame["low"].to_numpy(dtype=float).tolist()
    closes = frame["close"].to_numpy(dtype=float).tolist()
    values = _atr_values(highs, lows, closes, period)
    return _with_column(frame, column_name or f"atr_{period}", values)


def _close_prices(frame: "pd.DataFrame") -> list[float]:
    if "close" not in frame.columns:
        raise KeyError("missing required price column: close")
    return frame["close"].to_numpy(dtype=float).tolist()


def _with_column(frame: "pd.DataFrame", column_name: str, values: list[float | None]) -> "pd.DataFrame":
    if len(frame.index) != len(values):
        raise ValueError("indicator output must match input rows")

    result = frame.copy(deep=True)
    result[column_name] = values
    return result


def _validate_period(period: int) -> None:
    if period <= 0:
        raise ValueError("period must be positive")


def _sma_values(prices: list[float], period: int) -> list[float | None]:
    _validate_period(period)
    result: list[float | None] = [None] * len(prices)
    if len(prices) < period:
        return result

    running_total = sum(prices[:period])
    result[period - 1] = running_total / period

    for index in range(period, len(prices)):
        running_total += prices[index] - prices[index - period]
        result[index] = running_total / period

    return result


def _ema_values(prices: list[float], period: int) -> list[float | None]:
    _validate_period(period)
    result: list[float | None] = [None] * len(prices)
    if len(prices) < period:
        return result

    multiplier = 2.0 / (period + 1.0)
    current = sum(prices[:period]) / period
    result[period - 1] = current

    for index in range(period, len(prices)):
        current = (prices[index] - current) * multiplier + current
        result[index] = current

    return result


def _rsi_values(prices: list[float], period: int) -> list[float | None]:
    _validate_period(period)
    result: list[float | None] = [None] * len(prices)
    if len(prices) <= period:
        return result

    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(prices)):
        change = prices[index] - prices[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, len(prices)):
        gain = gains[index - 1]
        loss = losses[index - 1]
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[index] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0

    relative_strength = avg_gain / avg_loss
    value = 100.0 - (100.0 / (1.0 + relative_strength))
    return max(0.0, min(100.0, value))


def _atr_values(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int,
) -> list[float | None]:
    _validate_period(period)
    n = len(highs)
    result: list[float | None] = [None] * n
    if len(lows) != n or len(closes) != n or n < period + 1:
        return result

    true_ranges: list[float] = []
    for index in range(1, n):
        true_range = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        true_ranges.append(true_range)

    current = sum(true_ranges[:period]) / period
    result[period] = current
    for index in range(period, len(true_ranges)):
        current = (current * (period - 1) + true_ranges[index]) / period
        result[index + 1] = current

    return result


def add_macd(
    frame: "pd.DataFrame",
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> "pd.DataFrame":
    """Add MACD (Moving Average Convergence Divergence) indicator.

    Returns frame with columns:
    - macd_line: Fast EMA - Slow EMA
    - macd_signal: EMA of MACD line
    - macd_histogram: MACD line - Signal line
    """
    prices = _close_prices(frame)
    fast_ema = _ema_values(prices, fast_period)
    slow_ema = _ema_values(prices, slow_period)

    # MACD line = Fast EMA - Slow EMA
    macd_line: list[float | None] = [None] * len(prices)
    for i in range(len(prices)):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd_line[i] = fast_ema[i] - slow_ema[i]

    # Signal line = EMA of MACD line
    valid_macd = [v for v in macd_line if v is not None]
    signal_line: list[float | None] = [None] * len(prices)
    if len(valid_macd) >= signal_period:
        signal_values = _ema_values(valid_macd, signal_period)
        # Align signal values with original index
        offset = len(prices) - len(valid_macd)
        for i in range(len(signal_values)):
            if signal_values[i] is not None:
                signal_line[i + offset] = signal_values[i]

    # Histogram = MACD line - Signal line
    histogram: list[float | None] = [None] * len(prices)
    for i in range(len(prices)):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]

    result = _with_column(frame, "macd_line", macd_line)
    result = _with_column(result, "macd_signal", signal_line)
    result = _with_column(result, "macd_histogram", histogram)
    return result


def add_bollinger_bands(
    frame: "pd.DataFrame",
    period: int = 20,
    std_dev: float = 2.0,
) -> "pd.DataFrame":
    """Add Bollinger Bands indicator.

    Returns frame with columns:
    - bb_middle: SMA (middle band)
    - bb_upper: Middle + (std_dev * std)
    - bb_lower: Middle - (std_dev * std)
    - bb_width: (Upper - Lower) / Middle * 100
    - bb_percent_b: (Close - Lower) / (Upper - Lower) * 100
    """
    prices = _close_prices(frame)
    middle = _sma_values(prices, period)

    # Calculate standard deviation
    std_values: list[float | None] = [None] * len(prices)
    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1 : i + 1]
        if len(window) == period:
            mean = sum(window) / period
            variance = sum((p - mean) ** 2 for p in window) / period
            std_values[i] = variance**0.5

    # Calculate bands
    upper: list[float | None] = [None] * len(prices)
    lower: list[float | None] = [None] * len(prices)
    width: list[float | None] = [None] * len(prices)
    percent_b: list[float | None] = [None] * len(prices)

    for i in range(len(prices)):
        if middle[i] is not None and std_values[i] is not None:
            upper[i] = middle[i] + (std_dev * std_values[i])
            lower[i] = middle[i] - (std_dev * std_values[i])

            # Band width as percentage of middle band
            if middle[i] != 0:
                width[i] = ((upper[i] - lower[i]) / middle[i]) * 100

            # %B indicator: where price is within the bands
            band_range = upper[i] - lower[i]
            if band_range != 0:
                percent_b[i] = ((prices[i] - lower[i]) / band_range) * 100

    result = _with_column(frame, "bb_middle", middle)
    result = _with_column(result, "bb_upper", upper)
    result = _with_column(result, "bb_lower", lower)
    result = _with_column(result, "bb_width", width)
    result = _with_column(result, "bb_percent_b", percent_b)
    return result


def add_vwap(frame: "pd.DataFrame") -> "pd.DataFrame":
    """Add VWAP (Volume Weighted Average Price) indicator.

    VWAP = cumulative(typical_price * volume) / cumulative(volume)
    where typical_price = (high + low + close) / 3

    Returns frame with column:
    - vwap: Volume weighted average price
    """
    required_columns = {"high", "low", "close", "volume"}
    if not required_columns.issubset(frame.columns):
        raise KeyError("missing required columns: high, low, close, volume")

    highs = frame["high"].to_numpy(dtype=float).tolist()
    lows = frame["low"].to_numpy(dtype=float).tolist()
    closes = frame["close"].to_numpy(dtype=float).tolist()
    volumes = frame["volume"].to_numpy(dtype=float).tolist()

    n = len(closes)
    vwap: list[float | None] = [None] * n

    cum_typical_volume = 0.0
    cum_volume = 0.0

    for i in range(n):
        typical_price = (highs[i] + lows[i] + closes[i]) / 3.0
        cum_typical_volume += typical_price * volumes[i]
        cum_volume += volumes[i]

        if cum_volume > 0:
            vwap[i] = cum_typical_volume / cum_volume

    return _with_column(frame, "vwap", vwap)


def add_stochastic(
    frame: "pd.DataFrame",
    k_period: int = 14,
    d_period: int = 3,
) -> "pd.DataFrame":
    """Add Stochastic Oscillator (%K and %D).

    %K = (Close - Lowest Low) / (Highest High - Lowest Low) * 100
    %D = SMA of %K

    Returns frame with columns:
    - stoch_k: Fast stochastic
    - stoch_d: Slow stochastic (signal)
    """
    required_columns = {"high", "low", "close"}
    if not required_columns.issubset(frame.columns):
        raise KeyError("missing required columns: high, low, close")

    highs = frame["high"].to_numpy(dtype=float).tolist()
    lows = frame["low"].to_numpy(dtype=float).tolist()
    closes = frame["close"].to_numpy(dtype=float).tolist()

    n = len(closes)
    k_values: list[float | None] = [None] * n
    d_values: list[float | None] = [None] * n

    # Calculate %K
    for i in range(k_period - 1, n):
        window_high = max(highs[i - k_period + 1 : i + 1])
        window_low = min(lows[i - k_period + 1 : i + 1])
        range_hl = window_high - window_low

        if range_hl > 0:
            k_values[i] = ((closes[i] - window_low) / range_hl) * 100.0
        else:
            k_values[i] = 50.0

    # Calculate %D (SMA of %K)
    valid_k = [v for v in k_values if v is not None]
    if len(valid_k) >= d_period:
        d_sma = _sma_values([v if v is not None else 0.0 for v in k_values], d_period)
        for i in range(n):
            if k_values[i] is not None:
                d_values[i] = d_sma[i]

    result = _with_column(frame, "stoch_k", k_values)
    result = _with_column(result, "stoch_d", d_values)
    return result


def add_adx(
    frame: "pd.DataFrame",
    period: int = 14,
) -> "pd.DataFrame":
    """Add Average Directional Index (ADX).

    Measures trend strength regardless of direction.
    ADX > 25 indicates strong trend, < 20 indicates range.

    Returns frame with columns:
    - adx: Average Directional Index
    - plus_di: +DI (Positive Directional Indicator)
    - minus_di: -DI (Negative Directional Indicator)
    """
    required_columns = {"high", "low", "close"}
    if not required_columns.issubset(frame.columns):
        raise KeyError("missing required columns: high, low, close")

    highs = frame["high"].to_numpy(dtype=float).tolist()
    lows = frame["low"].to_numpy(dtype=float).tolist()
    closes = frame["close"].to_numpy(dtype=float).tolist()

    n = len(closes)
    if n < period + 1:
        result = _with_column(frame, "adx", [None] * n)
        result = _with_column(result, "plus_di", [None] * n)
        result = _with_column(result, "minus_di", [None] * n)
        return result

    # Calculate +DM, -DM, and TR
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_values: list[float] = []

    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)

        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_values.append(tr)

    # Calculate smoothed values
    atr = sum(tr_values[:period]) / period
    plus_di_val = 100 * sum(plus_dm[:period]) / (period * atr) if atr > 0 else 0
    minus_di_val = 100 * sum(minus_dm[:period]) / (period * atr) if atr > 0 else 0

    adx_values: list[float | None] = [None] * n
    plus_di_values: list[float | None] = [None] * n
    minus_di_values: list[float | None] = [None] * n

    # First ADX value
    if plus_di_val + minus_di_val > 0:
        dx = 100 * abs(plus_di_val - minus_di_val) / (plus_di_val + minus_di_val)
    else:
        dx = 0
    adx_values[period] = dx
    plus_di_values[period] = plus_di_val
    minus_di_values[period] = minus_di_val

    # Smooth subsequent values
    for i in range(period, len(tr_values)):
        atr = (atr * (period - 1) + tr_values[i]) / period
        plus_smooth = (plus_smooth * (period - 1) + plus_dm[i]) / period if i > period else sum(plus_dm[:period])
        minus_smooth = (minus_smooth * (period - 1) + minus_dm[i]) / period if i > period else sum(minus_dm[:period])

        if i > period:
            plus_di_val = 100 * plus_smooth / atr if atr > 0 else 0
            minus_di_val = 100 * minus_smooth / atr if atr > 0 else 0

            if plus_di_val + minus_di_val > 0:
                dx = 100 * abs(plus_di_val - minus_di_val) / (plus_di_val + minus_di_val)
            else:
                dx = 0

            adx_values[i + 1] = dx
            plus_di_values[i + 1] = plus_di_val
            minus_di_values[i + 1] = minus_di_val

    result = _with_column(frame, "adx", adx_values)
    result = _with_column(result, "plus_di", plus_di_values)
    result = _with_column(result, "minus_di", minus_di_values)
    return result


def add_williams_r(
    frame: "pd.DataFrame",
    period: int = 14,
) -> "pd.DataFrame":
    """Add Williams %R (Williams Percent Range).

    Momentum indicator measuring overbought/oversold levels.
    Range: -100 (oversold) to 0 (overbought).

    Returns frame with column:
    - williams_r: Williams %R
    """
    required_columns = {"high", "low", "close"}
    if not required_columns.issubset(frame.columns):
        raise KeyError("missing required columns: high, low, close")

    highs = frame["high"].to_numpy(dtype=float).tolist()
    lows = frame["low"].to_numpy(dtype=float).tolist()
    closes = frame["close"].to_numpy(dtype=float).tolist()

    n = len(closes)
    wr_values: list[float | None] = [None] * n

    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1 : i + 1])
        window_low = min(lows[i - period + 1 : i + 1])
        range_hl = window_high - window_low

        if range_hl > 0:
            wr_values[i] = -100.0 * (window_high - closes[i]) / range_hl
        else:
            wr_values[i] = -50.0

    return _with_column(frame, "williams_r", wr_values)


def add_obv(frame: "pd.DataFrame") -> "pd.DataFrame":
    """Add On-Balance Volume (OBV).

    Cumulative volume flow indicator.
    Rising OBV confirms uptrend, falling OBV confirms downtrend.

    Returns frame with column:
    - obv: On-Balance Volume
    """
    required_columns = {"close", "volume"}
    if not required_columns.issubset(frame.columns):
        raise KeyError("missing required columns: close, volume")

    closes = frame["close"].to_numpy(dtype=float).tolist()
    volumes = frame["volume"].to_numpy(dtype=float).tolist()

    n = len(closes)
    obv_values: list[float | None] = [None] * n

    if n == 0:
        return _with_column(frame, "obv", obv_values)

    obv = float(volumes[0])
    obv_values[0] = obv

    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
        # If close == previous close, OBV unchanged
        obv_values[i] = obv

    return _with_column(frame, "obv", obv_values)


def add_atr_percent(
    frame: "pd.DataFrame",
    period: int = 14,
) -> "pd.DataFrame":
    """Add ATR as percentage of price (volatility measure).

    ATR% = (ATR / Close) * 100
    Higher values indicate higher volatility.

    Returns frame with columns:
    - atr_{period}: Absolute ATR value
    - atr_pct: ATR as percentage of price
    """
    required_columns = {"high", "low", "close"}
    if not required_columns.issubset(frame.columns):
        raise KeyError("missing required columns: high, low, close")

    # First add regular ATR
    result = add_atr(frame, period)

    closes = frame["close"].to_numpy(dtype=float).tolist()
    atr_col = f"atr_{period}"
    atr_values = result[atr_col].tolist()

    atr_pct_values: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if atr_values[i] is not None and closes[i] > 0:
            atr_pct_values[i] = (atr_values[i] / closes[i]) * 100.0

    result = _with_column(result, "atr_pct", atr_pct_values)
    return result
