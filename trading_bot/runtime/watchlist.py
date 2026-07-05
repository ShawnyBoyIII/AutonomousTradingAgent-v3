from __future__ import annotations

from pathlib import Path


def normalize_symbol(symbol: str) -> str:
    value = symbol.upper().strip()
    if not value or len(value) > 12 or not all(ch.isalnum() or ch in ".-" for ch in value):
        raise ValueError("invalid symbol")
    return value


def read_watchlist(path: str | Path) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    symbols: list[str] = []
    seen: set[str] = set()
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for raw_symbol in line.split(","):
            try:
                symbol = normalize_symbol(raw_symbol)
            except ValueError:
                continue
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols


def write_watchlist(path: str | Path, symbols: list[str]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("".join(f"{symbol}\n" for symbol in symbols), encoding="utf-8")


def add_symbol(path: str | Path, symbol: str) -> list[str]:
    value = normalize_symbol(symbol)
    symbols = read_watchlist(path)
    if value not in symbols:
        symbols.append(value)
        write_watchlist(path, symbols)
    return symbols


def remove_symbol(path: str | Path, symbol: str) -> list[str]:
    value = normalize_symbol(symbol)
    symbols = [item for item in read_watchlist(path) if item != value]
    write_watchlist(path, symbols)
    return symbols
