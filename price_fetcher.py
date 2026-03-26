"""
price_fetcher.py — Unified price resolver for the Solana AI Trading Agent.

Three-source fallback strategy:
  1. Bitget spot API  (SYMBOL+USDT pair) — fast, works for major coins
  2. Bitget Wallet Skill token-price     — works for meme coins with a contract
  3. Bitget Wallet Skill search-tokens   — last-resort keyword search

Returns (price: float | None, source: str)
"""

import requests
import subprocess
import sys
import json
import os


def _bitget_spot(symbol: str):
    """Try Bitget centralised spot market (e.g. WIFUSDT)."""
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/spot/market/tickers",
            params={"symbol": f"{symbol}USDT"},
            timeout=5
        )
        d = r.json()
        if d.get("data") and len(d["data"]) > 0:
            price = float(d["data"][0]["lastPr"])
            if price > 0:
                return price, "bitget_spot"
    except Exception:
        pass
    return None, None


def _bitget_skill_contract(contract: str):
    """Try Bitget Wallet Skill token-price with an on-chain contract address."""
    if not contract:
        return None, None
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "scripts/bitget_agent_api.py",
             "token-price",
             "--chain", "sol",
             "--contract", contract],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=10
        )
        data = json.loads(result.stdout.strip())
        if "price" in data:
            price = float(data["price"])
            if price > 0:
                return price, "skill_contract"
    except Exception:
        pass
    return None, None


def _bitget_skill_search(symbol: str):
    """Try Bitget Wallet Skill search-tokens keyword search as last resort."""
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "scripts/bitget_agent_api.py",
             "search-tokens",
             "--chain", "sol",
             "--keyword", symbol],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=10
        )
        data = json.loads(result.stdout.strip())

        # API response: {"data": {"list": [...], ...}, "status": 0}
        items = data.get("data", {}).get("list", [])
        if not isinstance(items, list):
            items = []

        # Prefer exact symbol match, fall back to first result
        match = None
        for item in items:
            if item.get("symbol", "").lower() == symbol.lower():
                match = item
                break
        if not match and items:
            match = items[0]

        if match:
            price = float(match.get("price", 0))
            if price > 0:
                return price, "skill_search"
    except Exception:
        pass
    return None, None


def get_token_price(symbol: str, contract: str = "") -> tuple:
    """
    Resolve the current price for a token using a 3-source fallback.

    Args:
        symbol:   Token ticker, e.g. "WIF", "BONK", "CHIBI"
        contract: Solana contract address (optional but strongly recommended
                  for meme coins not listed on Bitget spot)

    Returns:
        (price, source)  where price is float or None, source is a string
        describing which API provided the price.
    """
    # Source 1 — Bitget spot (fast, works for listed tokens)
    price, source = _bitget_spot(symbol)
    if price:
        return price, source

    # Source 2 — Bitget Wallet Skill contract price (meme coins)
    price, source = _bitget_skill_contract(contract)
    if price:
        return price, source

    # Source 3 — Bitget Wallet Skill keyword search (last resort)
    price, source = _bitget_skill_search(symbol)
    if price:
        return price, source

    return None, "unavailable"


def get_token_price_with_fallback(symbol: str, contract: str = "",
                                  fallback_price: float = 0.0) -> tuple:
    """
    Same as get_token_price but returns (fallback_price, False) instead of
    (None, ...) when no live price is found.

    Returns:
        (price: float, is_live: bool)
    """
    price, source = get_token_price(symbol, contract)
    if price and price > 0:
        return price, True
    return fallback_price, False


if __name__ == "__main__":
    import sys as _sys

    test_symbols = [
        ("SOL",  ""),
        ("WIF",  "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
        ("BONK", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"),
        ("CHIBI",""),      # meme coin — will test fallback sources
    ]

    print("=" * 60)
    print("  price_fetcher.py — Self-Test")
    print("=" * 60)
    for sym, con in test_symbols:
        price, source = get_token_price(sym, con)
        if price:
            print(f"  {sym:<8} ${price:<16} via {source}")
        else:
            print(f"  {sym:<8} price unavailable")
    print("=" * 60)
