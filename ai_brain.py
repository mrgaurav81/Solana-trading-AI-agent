from groq import Groq
from market_scanner import run_scanner
from bitget_skill import check_token_security
import subprocess
import json
import sys
import os



from dotenv import load_dotenv
load_dotenv()
API_KEY = os.environ.get("GROQ_API_KEY", "")
client  = Groq(api_key=API_KEY)


def get_price_history(contract, chain="sol"):
    """
    Gets 24h price history from Bitget Wallet Skill API.
    Gives AI much more context about price trends.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        result = subprocess.run(
            [sys.executable, "scripts/bitget_agent_api.py",
             "kline",
             "--chain"   , chain,
             "--contract", contract,
             "--type"    , "1H",
             "--limit"   , "24"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30
        )
        data = json.loads(result.stdout.strip())

        if "data" in data and data["data"]:
            candles = data["data"]
            closes  = [float(c.get("close",  0)) for c in candles if c.get("close")]
            volumes = [float(c.get("volume", 0)) for c in candles if c.get("volume")]
            highs   = [float(c.get("high",   0)) for c in candles if c.get("high")]
            lows    = [float(c.get("low",    0)) for c in candles if c.get("low")]

            if len(closes) >= 2:
                price_trend = "UP"        if closes[-1]  > closes[0]  else "DOWN"
                vol_trend   = "INCREASING" if volumes[-1] > volumes[0] else "DECREASING"
                price_range = max(highs) - min(lows)
                avg_volume  = sum(volumes) / len(volumes)

                return {
                    "closes"     : closes[-6:],
                    "price_trend": price_trend,
                    "vol_trend"  : vol_trend,
                    "price_range": round(price_range, 8),
                    "avg_volume" : round(avg_volume, 2),
                    "candles"    : len(closes)
                }

    except Exception as e:
        print(f"   History error for {contract[:10]}: {e}")

    return None


def determine_strategy(token, history):
    """
    Determines best trading strategy for a token.

    3 Strategies:
    MOMENTUM - Strong uptrend with increasing volume
    DCA      - Steady growth good for small regular buys
    BREAKOUT - Price breaking out of recent range
    """
    change = float(token.get("change_24h_pct",
                   token.get("change_24h", 0)))

    if isinstance(change, float) and abs(change) < 1:
        change = change * 100

    if not history:
        if change > 5:
            return "MOMENTUM"
        elif change > 1:
            return "DCA"
        else:
            return "NEUTRAL"

    price_trend = history.get("price_trend", "DOWN")
    vol_trend   = history.get("vol_trend",   "DECREASING")
    closes      = history.get("closes",      [])

    # Momentum: strong uptrend + increasing volume
    if price_trend == "UP" and vol_trend == "INCREASING" and change > 3:
        return "MOMENTUM"

    # Breakout: price near recent high with volume surge
    if len(closes) >= 3:
        recent_high = max(closes[-3:])
        current     = closes[-1]
        if current >= recent_high * 0.98 and vol_trend == "INCREASING":
            return "BREAKOUT"

    # DCA: steady uptrend even without huge volume
    if price_trend == "UP" and change > 1:
        return "DCA"

    return "NEUTRAL"


def build_smart_prompt(safe_tokens, strategies, histories):
    """
    Builds a rich detailed prompt for AI with:
    - Price history context
    - Strategy recommendations
    - Clear decision framework
    """
    token_analysis = ""

    for token in safe_tokens[:5]:   # analyze up to 5 tokens
        symbol   = token.get("symbol",       "")
        price    = token.get("price",         0)
        change   = token.get("change_24h_pct",
                   token.get("change_24h",    0))
        volume   = token.get("volume_24h",
                   token.get("turnover_24h",  0))
        security = token.get("security",      "UNKNOWN")
        strategy = strategies.get(symbol,     "NEUTRAL")
        history  = histories.get(symbol)

        history_text = ""
        if history:
            history_text = f"""
    Price Trend  : {history.get('price_trend')}
    Volume Trend : {history.get('vol_trend')}
    Last 6 closes: {history.get('closes', [])}
    Avg Volume   : {history.get('avg_volume')}"""

        token_analysis += f"""
TOKEN: {symbol}
    Current Price : ${price}
    24h Change    : {change}%
    Volume        : ${float(volume):,.0f}
    Security      : {security} (verified by Bitget Wallet Skill API)
    Strategy      : {strategy}
    {history_text}
---"""

    prompt = f"""
You are an aggressive Solana meme-coin trading agent optimising for profit.
You have real-time data from Bitget Wallet Skill API.

MARKET DATA:
{token_analysis}

TRADING STRATEGIES:
1. MOMENTUM - Strong uptrend + increasing volume. Position $8-10
2. DCA      - Steady grower, small position $3-5
3. BREAKOUT - Breaking new highs on volume. Position $5-8
4. SKIP     - Only skip if signal is clearly negative or security is DANGEROUS

DECISION RULES:
- MOMENTUM tokens : BUY if change > 1.5% AND volume trend is INCREASING
- DCA tokens      : BUY with small amount if change > 0.5% AND price trend is UP
- BREAKOUT tokens : BUY if price near recent high AND volume surging
- SKIP only if    : change < 0% OR security is DANGEROUS
- WARNING security: trade with smaller size, NOT a reason to skip entirely
- Meme coins are volatile — embrace calculated risk, do not over-filter

For EACH token provide EXACTLY this format:

TOKEN: [symbol]
STRATEGY: [MOMENTUM or DCA or BREAKOUT or SKIP]
DECISION: [BUY or HOLD or SKIP]
CONFIDENCE: [HIGH or MEDIUM or LOW]
REASON: [2 sentence analysis]
SUGGESTED_AMOUNT_USDT: [amount between 3 and 10, or 0 if skip]
RISK_LEVEL: [LOW or MEDIUM or HIGH]

End with:
BEST_PICK: [symbol of best opportunity or NONE]
MARKET_SUMMARY: [one sentence about overall Solana market]
"""
    return prompt


def _is_suspicious_name(symbol: str, name: str = "") -> bool:
    """Returns True if symbol or name contains known rug/scam keywords."""
    BAD_WORDS = [
        "fake", "scam", "rug", "honeypot",
        "drain", "ponzi"
    ]
    combined = (symbol + " " + name).lower()
    return any(w in combined for w in BAD_WORDS)


def filter_safe_tokens(bullish_tokens):
    """
    Runs pre-filters + Bitget Wallet Skill security check on all tokens.

    Pre-filters (fast, no API call):
      1. Suspicious name keywords (scam, rug, honeypot, …)
      2. Market cap < $100,000
      3. Token age < 7 days (if issue_date field is present)
      4. Volume collapsed >50% vs hourly average (if volume_1h field present)

    Then removes any token flagged DANGEROUS by the security API.
    """
    from datetime import datetime as _dt

    print("\nRunning pre-filters + security checks...")
    print("-" * 60)

    safe_tokens = []

    for token in bullish_tokens:
        symbol   = token.get("symbol", "N/A")
        name     = token.get("name", "")
        contract = token.get("contract", "")

        # Pre-filter 1: suspicious name
        if _is_suspicious_name(symbol, name):
            print(f"   REMOVED {symbol} - suspicious name")
            continue

        # Pre-filter 2: market cap floor
        market_cap = float(token.get("market_cap", token.get("marketCap", 0)) or 0)
        if 0 < market_cap < 100_000:
            print(f"   REMOVED {symbol} - market cap too low (${market_cap:,.0f})")
            continue

        # Pre-filter 3: token age < 7 days
        issue_date_str = token.get("issue_date", token.get("issueDate", ""))
        if issue_date_str:
            try:
                issue_date = _dt.fromisoformat(str(issue_date_str)[:10])
                age_days   = (_dt.now() - issue_date).days
                if age_days < 7:
                    print(f"   REMOVED {symbol} - token too new ({age_days} days old)")
                    continue
            except Exception:
                pass

        # Pre-filter 4: volume collapse > 50% in last hour
        vol_24h = float(token.get("volume_24h", token.get("turnover_24h", 0)) or 0)
        vol_1h  = float(token.get("volume_1h", 0) or 0)
        if vol_24h > 0 and vol_1h > 0:
            hourly_avg = vol_24h / 24
            if vol_1h < hourly_avg * 0.5:
                print(f"   REMOVED {symbol} - volume collapsed "
                      f"({vol_1h:.0f} vs avg {hourly_avg:.0f}/h)")
                continue

        # Security API check
        if not contract:
            print(f"   {symbol} - No contract, skipping security check")
            token["security"] = "UNVERIFIED"
            safe_tokens.append(token)
            continue

        security = check_token_security(contract=contract)
        verdict  = security.get("verdict", "UNKNOWN")
        reason   = security.get("reason",  "")

        print(f"   {symbol} - {verdict} - {reason}")

        if verdict == "DANGEROUS":
            print(f"   REMOVED {symbol} - dangerous token!")
            continue

        token["security"] = verdict
        safe_tokens.append(token)

    print(f"\n   {len(safe_tokens)}/{len(bullish_tokens)} tokens passed all filters")
    return safe_tokens


def ask_groq_smart(safe_tokens, strategies, histories):
    """
    Sends enriched market data to Groq AI.
    Uses smarter prompt with history and strategy context.
    """
    if not safe_tokens:
        return None

    prompt = build_smart_prompt(safe_tokens, strategies, histories)

    print("\nSending enriched data to Groq AI...")
    print(f"Strategies detected: {list(strategies.values())}")

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role"   : "system",
                "content": """You are an aggressive Solana meme-coin trader
who loves calculated risk. You analyse price trends and volume patterns to
find profit opportunities. Always respond in the exact structured format
requested. Bias toward BUY when signals are positive, SKIP only when clearly
negative. Be concise."""
            },
            {
                "role"   : "user",
                "content": prompt
            }
        ],
        max_tokens=2048,
        temperature=0.5
    )

    return response.choices[0].message.content


def parse_decisions(response_text):
    """
    Parses AI response into structured decisions.
    Extracts strategy, risk level and market summary.
    """
    if not response_text:
        return []

    decisions = []
    lines     = response_text.split("\n")
    current   = {}

    for line in lines:
        line = line.strip()

        if line.startswith("TOKEN:"):
            if current:
                decisions.append(current)
            current = {"token": line.replace("TOKEN:", "").strip()}

        elif line.startswith("STRATEGY:"):
            current["strategy"] = line.replace("STRATEGY:", "").strip()

        elif line.startswith("DECISION:"):
            current["decision"] = line.replace("DECISION:", "").strip()

        elif line.startswith("CONFIDENCE:"):
            current["confidence"] = line.replace("CONFIDENCE:", "").strip()

        elif line.startswith("REASON:"):
            current["reason"] = line.replace("REASON:", "").strip()

        elif line.startswith("SUGGESTED_AMOUNT_USDT:"):
            current["amount"] = line.replace("SUGGESTED_AMOUNT_USDT:", "").strip()

        elif line.startswith("RISK_LEVEL:"):
            current["risk"] = line.replace("RISK_LEVEL:", "").strip()

        elif line.startswith("BEST_PICK:"):
            if current:
                decisions.append(current)
            best = line.replace("BEST_PICK:", "").strip()
            decisions.append({"best_pick": best})

        elif line.startswith("MARKET_SUMMARY:"):
            summary = line.replace("MARKET_SUMMARY:", "").strip()
            decisions.append({"market_summary": summary})

    return decisions


def print_decisions(decisions):
    """
    Prints AI decisions in clean detailed format.
    """
    print("\n" + "=" * 60)
    print("  SMART AI TRADING DECISIONS v2")
    print("=" * 60)

    best_pick = None
    summary   = None

    for d in decisions:

        if "best_pick" in d:
            best_pick = d["best_pick"]
            continue

        if "market_summary" in d:
            summary = d["market_summary"]
            continue

        risk_emoji = {
            "LOW"   : "✅",
            "MEDIUM": "⚠️",
            "HIGH"  : "🚨"
        }.get(d.get("risk", ""), "❓")

        print(f"\nToken      : {d.get('token',      'N/A')}")
        print(f"Strategy   : {d.get('strategy',   'N/A')}")
        print(f"Decision   : {d.get('decision',   'N/A')}")
        print(f"Confidence : {d.get('confidence', 'N/A')}")
        print(f"Risk       : {risk_emoji} {d.get('risk', 'N/A')}")
        print(f"Amount     : ${d.get('amount',    'N/A')}")
        print(f"Reason     : {d.get('reason',     'N/A')}")
        print("-" * 40)

    if summary:
        print(f"\nMarket Summary : {summary}")

    if best_pick:
        print(f"Best Opportunity: {best_pick}")

    print("\n" + "=" * 60)
    return best_pick


def run_ai_brain():
    """
    Full smart AI pipeline:
    1. Scan market via Bitget Hotpicks
    2. Security filter via Bitget Wallet Skill API
    3. Get 24h price history for context
    4. Determine strategy per token
    5. Ask Groq AI with rich context
    6. Return structured decisions
    """

    # Step 1 - scan market
    print("\nStep 1 - Scanning Solana market...")
    bullish_tokens = run_scanner()

    if not bullish_tokens:
        print("\nNo bullish tokens found. Try again later.")
        return None, None

    # Step 2 - security filter
    print("\nStep 2 - Security filtering via Bitget Wallet Skill API...")
    safe_tokens = filter_safe_tokens(bullish_tokens)

    if not safe_tokens:
        print("\nNo tokens passed security check!")
        return None, None

    # Step 3 - get price history and strategies
    print("\nStep 3 - Fetching price history and strategies...")
    strategies = {}
    histories  = {}

    for token in safe_tokens[:3]:
        symbol   = token.get("symbol",   "")
        contract = token.get("contract", "")

        print(f"   Getting history for {symbol}...")
        history            = get_price_history(contract) if contract else None
        histories[symbol]  = history
        strategy           = determine_strategy(token, history)
        strategies[symbol] = strategy
        print(f"   {symbol} - Strategy: {strategy}")

    # Step 4 - ask AI
    print("\nStep 4 - Getting smart AI decisions...")
    response = ask_groq_smart(safe_tokens, strategies, histories)

    if not response:
        print("\nNo response from AI.")
        return None, None

    print("\n--- GROQ SMART RESPONSE ---")
    print(response)
    print("--- END ---\n")

    # Step 5 - parse and display
    decisions = parse_decisions(response)
    best_pick = print_decisions(decisions)

    return decisions, best_pick


if __name__ == "__main__":
    print("=" * 60)
    print("  Solana AI Trading Agent - Smart AI Brain v2")
    print("=" * 60)
    run_ai_brain()