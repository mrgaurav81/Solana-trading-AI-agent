import requests
from datetime import datetime
from bitget_skill import get_hot_picks, get_token_price


def analyze_token(token):
    """
    Analyzes a token's 24h change and gives a signal.
    """
    change = float(token.get("change_24h", 0)) * 100

    if change >= 1:
        signal = "BULLISH"
        reason = f"Price up {round(change, 2)}% in 24h"
    elif change <= -1:
        signal = "BEARISH"
        reason = f"Price down {round(change, 2)}% in 24h"
    else:
        signal = "NEUTRAL"
        reason = f"Price change only {round(change, 2)}%"

    return signal, reason, round(change, 2)


def print_token_report(token, signal, reason, change):
    """
    Prints a clean report for one token.
    """
    if signal == "BULLISH":
        status = "GREEN"
    elif signal == "BEARISH":
        status = "RED"
    else:
        status = "YELLOW"

    symbol   = token.get("symbol", "N/A")
    price    = token.get("price", 0)
    volume   = token.get("volume_24h", token.get("turnover_24h", 0))

    print(f"\n{status} {symbol}")
    print(f"   Price      : ${price}")
    print(f"   24h Change : {change}%")
    print(f"   Volume     : ${float(volume):,.0f}")
    print(f"   Signal     : {signal}")
    print(f"   Reason     : {reason}")


def run_scanner():
    """
    Main scanner function:
    1. Gets hot Solana tokens from Bitget Wallet Skill Hotpicks
    2. Analyzes each token
    3. Returns bullish tokens for AI brain
    """
    print(f"\nScan time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Getting hot Solana tokens from Bitget Wallet Skill API...")
    print("-" * 60)

    # Get trending tokens from Bitget Wallet Skill Hotpicks
    hot_tokens = get_hot_picks()

    if not hot_tokens:
        # Fallback to Bitget spot API if hotpicks fails
        print("Hotpicks unavailable — using Bitget spot API fallback...")
        fallback_symbols = ["SOLUSDT", "JUPUSDT", "BONKUSDT", "WIFUSDT", "RAYUSDT"]
        hot_tokens = []

        for symbol in fallback_symbols:
            try:
                r    = requests.get(
                    "https://api.bitget.com/api/v2/spot/market/tickers",
                    params={"symbol": symbol},
                    timeout=10
                )
                data = r.json()["data"][0]
                hot_tokens.append({
                    "symbol"    : symbol.replace("USDT", ""),
                    "price"     : float(data["lastPr"]),
                    "change_24h": float(data["change24h"]),
                    "volume_24h": float(data["baseVolume"]),
                    "chain"     : "sol",
                    "contract"  : ""
                })
            except:
                pass

    bullish_tokens = []

    for token in hot_tokens:
        signal, reason, change = analyze_token(token)
        print_token_report(token, signal, reason, change)

        if signal == "BULLISH":
            # Add extra fields needed by AI brain
            token["change_24h_pct"] = change
            token["signal"]         = signal
            bullish_tokens.append(token)

    # Print summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)

    if bullish_tokens:
        print(f"\nBullish tokens found: {len(bullish_tokens)}")
        for t in bullish_tokens:
            print(f"   {t.get('symbol')} — up {t.get('change_24h_pct')}% — ${t.get('price')}")
        print("\nPassing to AI brain for decisions...")
    else:
        print("\nNo bullish tokens right now.")
        print("Agent will wait and scan again in 15 minutes.")

    print("\n" + "=" * 60)
    return bullish_tokens


if __name__ == "__main__":
    run_scanner()
'''

---

## ▶️ Test It

python market_scanner.py
'''