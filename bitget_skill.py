import subprocess
import json
import sys
import os


def run_command(args):
    """
    Runs official Bitget Wallet Skill API script with UTF-8 encoding.
    Forces UTF-8 to handle special characters in token names.
    """
    cmd = [sys.executable, "scripts/bitget_agent_api.py"] + args

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30
        )

        output = result.stdout.strip()

        try:
            return json.loads(output)
        except:
            return {"raw": output, "error": "Could not parse"}

    except Exception as e:
        return {"error": str(e)}


def get_hot_picks():
    """
    Gets trending Solana tokens from Bitget Wallet Skill Hotpicks.
    """
    result = run_command(["rankings", "--name", "Hotpicks"])

    try:
        all_tokens = result["data"]["list"]
        sol_tokens = [t for t in all_tokens if t.get("chain") == "sol"]
        print(f"   Found {len(all_tokens)} total, {len(sol_tokens)} on Solana")
        return sol_tokens
    except Exception as e:
        print(f"   Hotpicks error: {e}")
        return []


def get_top_gainers():
    """
    Gets top gaining Solana tokens.
    """
    result = run_command(["rankings", "--name", "topGainers"])

    try:
        all_tokens = result["data"]["list"]
        sol_tokens = [t for t in all_tokens if t.get("chain") == "sol"]
        return sol_tokens
    except Exception as e:
        print(f"   Top gainers error: {e}")
        return []


def get_token_price(contract=""):
    """
    Gets live price for a Solana token.
    contract="" = native SOL.
    """
    result = run_command([
        "token-price",
        "--chain"   , "sol",
        "--contract", contract
    ])

    if "price" in result:
        return {
            "symbol"  : result.get("symbol", ""),
            "price"   : float(result.get("price", 0)),
            "chain"   : "sol",
            "contract": contract
        }

    return {"error": "No price data", "raw": result}


def check_token_security(contract=""):
    """
    Runs security audit on a Solana token.
    Returns SAFE, WARNING or DANGEROUS verdict.
    """
    result = run_command([
        "security",
        "--chain"   , "sol",
        "--contract", contract
    ])

    try:
        data       = result["data"][0]
        risk_count = data.get("riskCount", 0)
        warn_count = data.get("warnCount", 0)
        high_risk  = data.get("highRisk", False)
        freeze     = data.get("freezeAuth", False)
        buy_tax    = data.get("buyTax", 0)
        sell_tax   = data.get("sellTax", 0)

        if high_risk or risk_count > 0:
            verdict = "DANGEROUS"
            reason  = f"High risk! riskCount={risk_count}"
        elif freeze or buy_tax > 5 or sell_tax > 5:
            verdict = "WARNING"
            reason  = f"Concerns: freeze={freeze}, tax={buy_tax}%"
        elif warn_count > 0:
            verdict = "WARNING"
            reason  = f"Minor warnings: {warn_count}"
        else:
            verdict = "SAFE"
            reason  = f"No risks. buyTax={buy_tax}%, sellTax={sell_tax}%"

        return {
            "verdict"   : verdict,
            "reason"    : reason,
            "risk_count": risk_count,
            "high_risk" : high_risk,
            "buy_tax"   : buy_tax,
            "sell_tax"  : sell_tax
        }

    except Exception as e:
        return {"verdict": "UNKNOWN", "reason": str(e)}


def run_pre_trade_checks(symbol, contract):
    """
    Runs all checks before trading:
    1. Get price
    2. Security audit
    Returns (is_safe, price_data)
    """
    print(f"\n   Pre-trade checks for {symbol}...")

    price_data = get_token_price(contract)
    if "error" in price_data:
        print(f"   Could not get price — skipping")
        return False, None

    print(f"   Price: ${price_data['price']}")

    security = check_token_security(contract)
    verdict  = security.get("verdict", "UNKNOWN")
    reason   = security.get("reason", "")
    print(f"   Security: {verdict} — {reason}")

    if verdict == "DANGEROUS":
        print(f"   SKIPPING — dangerous token!")
        return False, None

    return True, price_data


def get_swap_quote(from_contract="", from_symbol="USDC",
                   from_amount="5", to_contract="",
                   to_symbol="SOL", wallet_address="demo"):
    """
    Gets best swap quote using Bitget 110+ DEX aggregation.
    from_contract = USDC address by default
    to_contract   = token we want to buy
    """
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    if not from_contract:
        from_contract = USDC

    result = run_command([
        "quote",
        "--from-chain"   , "sol",
        "--from-contract", from_contract,
        "--from-symbol"  , from_symbol,
        "--from-amount"  , str(from_amount),
        "--to-chain"     , "sol",
        "--to-contract"  , to_contract,
        "--to-symbol"    , to_symbol,
        "--from-address" , wallet_address,
        "--to-address"   , wallet_address
    ])

    return result


if __name__ == "__main__":

    print("=" * 60)
    print("  Bitget Wallet Skill API - Final Test")
    print("=" * 60)

    # Test 1: SOL price
    print("\nTest 1: SOL live price...")
    price = get_token_price(contract="")
    print(f"   SOL Price: ${price.get('price', 'N/A')}")

    # Test 2: Hot picks
    print("\nTest 2: Hot picks on Solana...")
    hot = get_hot_picks()
    for t in hot[:3]:
        print(f"   {t.get('symbol')} — ${t.get('price')}")

    # Test 3: Security on WIF
    WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    print("\nTest 3: Security check on WIF...")
    sec = check_token_security(WIF)
    print(f"   Verdict: {sec['verdict']} — {sec['reason']}")

    # Test 4: Full pre-trade check
    print("\nTest 4: Pre-trade check on SOL...")
    safe, data = run_pre_trade_checks("SOL", "")
    print(f"   Safe to trade: {safe}")

    print("\n" + "=" * 60)
    print("  All tests done!")
    print("=" * 60)
'''

---

## ▶️ Run It
```
python bitget_skill.py
'''