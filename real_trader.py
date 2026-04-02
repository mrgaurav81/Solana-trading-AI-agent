"""
real_trader.py — Live on-chain trading via Bitget Wallet Skill API.

Flow per trade:
  1. get_processed_balance  → check real USDT balance on Solana
  2. quote                  → get best swap routes (110+ DEXes)
  3. confirm (no_gas)       → finalise quote, get orderId (gas from USDT)
  4. order_make_sign_send   → makeOrder + sign with Solana private key + send
  5. get_order_details      → poll until success / failure

Safety guards:
  - MAX_TRADE_USDT      : $1.50 per trade  (protects the $8.7 capital)
  - MAX_OPEN_POSITIONS  : 3 concurrent holdings
  - MIN_USDT_RESERVE    : $3.00 always kept in wallet (never trade it)
  - SLIPPAGE            : 1.0 %
"""

import json
import os
import sys
import time
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── wallet config ──────────────────────────────────────────────────
SOL_WALLET   = os.getenv("SOL_WALLET_ADDRESS", "")
SOL_PRIV_KEY = os.getenv("SOL_PRIVATE_KEY", "")

# ── safety config ──────────────────────────────────────────────────
MAX_TRADE_USDT     = 1.50   # max USDT to spend per single buy
MAX_OPEN_POSITIONS = 3      # max concurrent holdings
MIN_USDT_RESERVE   = 3.00   # minimum balance always kept untouched
SLIPPAGE           = "1.00" # 1 % slippage

# ── Solana token addresses ──────────────────────────────────────────
USDT_SOL = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"  # USDT on Solana

# ── portfolio / settings files (shared with paper_trader) ──────────
PORTFOLIO_FILE   = "portfolio.json"
SETTINGS_FILE    = "agent_settings.json"
SCRIPTS_DIR      = Path(__file__).parent / "scripts"


# ═══════════════════════════════════════════════════════════════════
#  Internal helpers
# ═══════════════════════════════════════════════════════════════════

def _run_api(args: list) -> dict:
    """Run scripts/bitget_agent_api.py with given args, return parsed JSON."""
    cmd = [sys.executable, str(SCRIPTS_DIR / "bitget_agent_api.py")] + args
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8",
            errors="replace", env=env, timeout=45
        )
        raw = result.stdout.strip()
        if not raw:
            return {"error": result.stderr.strip() or "empty response"}
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e)}


def _write_key_tempfile() -> str:
    """Write private key to a secure temp file; return its path."""
    fd, path = tempfile.mkstemp(prefix=".pk_sol_", dir=str(SCRIPTS_DIR))
    os.write(fd, SOL_PRIV_KEY.encode())
    os.close(fd)
    return path


def _load_settings() -> dict:
    defaults = {
        "stop_loss_pct"    : 5.0,
        "take_profit_pct"  : 15.0,
        "max_trade_amount" : MAX_TRADE_USDT,
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults


# ═══════════════════════════════════════════════════════════════════
#  Portfolio helpers  (mirrors paper_trader API)
# ═══════════════════════════════════════════════════════════════════

def load_portfolio() -> dict:
    """Load or create portfolio. Starting balance = real USDT balance."""
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    real_bal = get_real_usdt_balance()
    portfolio = {
        "usdt_balance"  : real_bal,
        "holdings"      : {},
        "trade_history" : [],
        "total_trades"  : 0,
        "winning_trades": 0,
        "created_at"    : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode"          : "REAL",
    }
    save_portfolio(portfolio)
    return portfolio


def save_portfolio(portfolio: dict):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)


def calculate_portfolio_value(portfolio: dict, current_prices: dict) -> float:
    total = portfolio["usdt_balance"]
    for symbol, h in portfolio["holdings"].items():
        price = current_prices.get(symbol, h.get("buy_price", 0))
        total += h["amount"] * price
    return total


def print_portfolio_status(portfolio: dict, current_prices: dict):
    settings   = _load_settings()
    start_bal  = portfolio.get("start_balance", portfolio["usdt_balance"])
    total_val  = calculate_portfolio_value(portfolio, current_prices)
    pnl        = total_val - start_bal

    print("\n" + "=" * 60)
    print("  REAL PORTFOLIO STATUS")
    print("=" * 60)
    print(f"\n   USDT Balance : ${portfolio['usdt_balance']:.4f}")
    print(f"   Total Value  : ${total_val:.4f}")
    sign = "+" if pnl >= 0 else ""
    print(f"   P&L          : {sign}${pnl:.4f}")
    print(f"   Total Trades : {portfolio['total_trades']}")

    if portfolio["holdings"]:
        print("\n   Open Positions:")
        for sym, h in portfolio["holdings"].items():
            price = current_prices.get(sym, h["buy_price"])
            val   = h["amount"] * price
            pos_pnl = val - h["amount"] * h["buy_price"]
            print(f"   {sym}: {h['amount']:.6f} tokens | "
                  f"Bought@${h['buy_price']:.8f} | "
                  f"Now@${price:.8f} | "
                  f"P&L ${pos_pnl:.4f}")
    else:
        print("\n   No open positions.")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════════
#  Balance checker
# ═══════════════════════════════════════════════════════════════════

def get_real_usdt_balance() -> float:
    """
    Fetch real on-chain USDT balance on Solana from Bitget API.
    Falls back to portfolio file value if API call fails.
    """
    if not SOL_WALLET:
        print("   [real_trader] SOL_WALLET_ADDRESS not set in .env")
        return 0.0

    result = _run_api([
        "get-processed-balance",
        "--chain"   , "sol",
        "--address" , SOL_WALLET,
        "--contract", USDT_SOL,
    ])

    try:
        # Structure: data=[{chain, address, list: {contract: {balance,...}}}]
        items = result["data"]
        if not isinstance(items, list):
            items = [items]
        for item in items:
            token_map = item.get("list", {})
            # Exact USDT contract match
            if USDT_SOL in token_map:
                bal = float(token_map[USDT_SOL].get("balance", 0))
                print(f"   [real_trader] Real USDT balance: ${bal:.4f}")
                return bal
            # Fallback: any non-native token with balance > 0
            for contract_key, info in token_map.items():
                if contract_key and float(info.get("balance", 0)) > 0:
                    bal = float(info.get("balance", 0))
                    print(f"   [real_trader] USDT balance (fallback {contract_key[:8]}...): ${bal:.4f}")
                    return bal
    except Exception as e:
        print(f"   [real_trader] Balance fetch error: {e} | raw: {result}")

    return 0.0


def sync_balance_from_chain(portfolio: dict) -> dict:
    """Update portfolio USDT balance from on-chain state."""
    real = get_real_usdt_balance()
    if real > 0:
        portfolio["usdt_balance"] = real
        save_portfolio(portfolio)
    return portfolio


# ═══════════════════════════════════════════════════════════════════
#  Stop-loss / take-profit  (same logic as paper_trader)
# ═══════════════════════════════════════════════════════════════════

def check_stop_loss_take_profit(portfolio: dict, current_prices: dict) -> list:
    settings = _load_settings()
    sl_pct   = settings.get("stop_loss_pct",   5.0)
    tp_pct   = settings.get("take_profit_pct", 15.0)
    to_sell  = []

    for symbol, h in portfolio["holdings"].items():
        buy_p  = h.get("buy_price",    0)
        cur_p  = current_prices.get(symbol, 0)
        if not buy_p or not cur_p:
            continue

        pct = ((cur_p - buy_p) / buy_p) * 100

        if pct <= -sl_pct:
            to_sell.append({"symbol": symbol, "reason": "STOP_LOSS",
                            "pct_change": round(pct, 2),
                            "buy_price": buy_p, "current_price": cur_p,
                            "fraction": 1.0})
        elif pct >= tp_pct:
            to_sell.append({"symbol": symbol, "reason": "TAKE_PROFIT",
                            "pct_change": round(pct, 2),
                            "buy_price": buy_p, "current_price": cur_p,
                            "fraction": 1.0})
    return to_sell


def run_stop_loss_check(portfolio: dict, current_prices: dict):
    print("\nRunning stop-loss / take-profit check...")
    to_sell = check_stop_loss_take_profit(portfolio, current_prices)

    if not to_sell:
        print("   All positions within safe range.")
        return portfolio, []

    sold = []
    for item in to_sell:
        portfolio, ok = execute_real_sell(portfolio, item["symbol"],
                                          item["current_price"])
        if ok:
            sold.append(item)
    return portfolio, sold


# ═══════════════════════════════════════════════════════════════════
#  Core trade execution
# ═══════════════════════════════════════════════════════════════════

def _poll_order(order_id: str, max_wait: int = 120) -> bool:
    """Poll getOrderDetails until status='success' or timeout."""
    print(f"   Polling order {order_id}...")
    for _ in range(max_wait // 6):
        time.sleep(6)
        resp = _run_api(["get-order-details", "--order-id", order_id])
        try:
            status = resp["data"]["details"]["status"]
            print(f"   Order status: {status}")
            if status == "success":
                return True
            if status in ("failed", "cancelled"):
                print(f"   Order {status}: {resp}")
                return False
        except Exception:
            pass
    print(f"   Order polling timed out after {max_wait}s")
    return False


def _get_best_quote(from_amount: str, to_contract: str, to_symbol: str) -> dict | None:
    """
    Step 1: Get best swap quote.
    Returns the best quoteResult dict or None.
    """
    resp = _run_api([
        "quote",
        "--from-chain"   , "sol",
        "--from-contract", USDT_SOL,
        "--from-symbol"  , "USDT",
        "--from-amount"  , from_amount,
        "--to-chain"     , "sol",
        "--to-contract"  , to_contract,
        "--to-symbol"    , to_symbol,
        "--from-address" , SOL_WALLET,
        "--to-address"   , SOL_WALLET,
        "--slippage"     , SLIPPAGE,
    ])

    if resp.get("status") != 0 or resp.get("error_code") != 0:
        print(f"   Quote failed: {resp.get('msg', resp)}")
        return None

    results = (resp.get("data") or {}).get("quoteResults", [])
    if not results:
        print("   No quote results returned.")
        return None

    # Pick first (best) route
    return results[0]


def _get_best_sell_quote(token_amount: str, from_contract: str,
                         from_symbol: str) -> dict | None:
    """Quote for selling a meme coin back to USDT."""
    resp = _run_api([
        "quote",
        "--from-chain"   , "sol",
        "--from-contract", from_contract,
        "--from-symbol"  , from_symbol,
        "--from-amount"  , token_amount,
        "--to-chain"     , "sol",
        "--to-contract"  , USDT_SOL,
        "--to-symbol"    , "USDT",
        "--from-address" , SOL_WALLET,
        "--to-address"   , SOL_WALLET,
        "--slippage"     , SLIPPAGE,
    ])

    if resp.get("status") != 0 or resp.get("error_code") != 0:
        print(f"   Sell quote failed: {resp.get('msg', resp)}")
        return None

    results = (resp.get("data") or {}).get("quoteResults", [])
    if not results:
        return None
    return results[0]


def _confirm_and_execute(
    from_amount: str, from_contract: str, from_symbol: str,
    to_contract: str, to_symbol: str,
    market: str, protocol: str, recommend_slippage: str,
    out_amount: str, key_file: str
) -> bool:
    """
    Steps 2–4: confirm → makeOrder → sign → send.
    Uses no_gas mode (gas deducted from fromToken, no native SOL needed).
    """
    # Step 2 — confirm
    confirm_args = [
        "confirm",
        "--from-chain"          , "sol",
        "--from-contract"       , from_contract,
        "--from-symbol"         , from_symbol,
        "--from-amount"         , from_amount,
        "--from-address"        , SOL_WALLET,
        "--to-chain"            , "sol",
        "--to-contract"         , to_contract,
        "--to-symbol"           , to_symbol,
        "--to-address"          , SOL_WALLET,
        "--market"              , market,
        "--protocol"            , protocol,
        "--slippage"            , recommend_slippage,
        "--recommend-slippage"  , recommend_slippage,
        "--last-out-amount"     , out_amount,
        "--features"            , "no_gas",       # ← gasless mode
        "--gas-level"           , "average",
    ]
    confirm_resp = _run_api(confirm_args)

    if confirm_resp.get("status") != 0 or confirm_resp.get("error_code") != 0:
        print(f"   Confirm failed: {confirm_resp.get('msg', confirm_resp)}")
        return False

    order_id = (confirm_resp.get("data") or {}).get("orderId")
    if not order_id:
        print(f"   No orderId in confirm response: {confirm_resp}")
        return False

    print(f"   Got orderId: {order_id}")

    # Steps 3+4 — makeOrder + sign + send (all in one script)
    sign_args = [
        sys.executable,
        str(SCRIPTS_DIR / "order_make_sign_send.py"),
        "--private-key-file-sol", key_file,
        "--from-address"        , SOL_WALLET,
        "--to-address"          , SOL_WALLET,
        "--order-id"            , order_id,
        "--from-chain"          , "sol",
        "--from-contract"       , from_contract,
        "--from-symbol"         , from_symbol,
        "--to-chain"            , "sol",
        "--to-contract"         , to_contract,
        "--to-symbol"           , to_symbol,
        "--from-amount"         , from_amount,
        "--slippage"            , recommend_slippage,
        "--market"              , market,
        "--protocol"            , protocol,
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            sign_args, capture_output=True,
            encoding="utf-8", errors="replace",
            env=env, timeout=90
        )
        raw = result.stdout.strip()
        print(f"   Sign+Send response: {raw[:300]}")
        if result.returncode != 0:
            print(f"   Sign+Send stderr: {result.stderr.strip()[:300]}")
            return False

        send_resp = json.loads(raw) if raw else {}
        if send_resp.get("status") != 0 or send_resp.get("error_code") != 0:
            print(f"   Send failed: {send_resp.get('msg', send_resp)}")
            return False

        # Step 5 — poll for completion
        return _poll_order(order_id)

    except Exception as e:
        print(f"   Sign+Send exception: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
#  Public buy / sell functions  (same signature as paper_trader)
# ═══════════════════════════════════════════════════════════════════

def execute_real_buy(portfolio: dict, token: dict, amount_usdt,
                     quote=None) -> tuple[dict, bool]:
    """
    Execute a real on-chain BUY:
      USDT (Solana) → meme coin
    """
    symbol   = token.get("symbol", "").replace("USDT", "")
    contract = token.get("contract", "")
    price    = float(token.get("price", 0))
    amount_u = float(amount_usdt)

    # ── safety checks ────────────────────────────────────────────
    if not SOL_WALLET or not SOL_PRIV_KEY:
        print("   [real_trader] Wallet not configured — aborting buy")
        return portfolio, False

    if not contract:
        print(f"   [real_trader] No contract for {symbol} — skipping")
        return portfolio, False

    if amount_u > MAX_TRADE_USDT:
        amount_u = MAX_TRADE_USDT
        print(f"   [real_trader] Capped trade to ${MAX_TRADE_USDT}")

    if portfolio["usdt_balance"] - amount_u < MIN_USDT_RESERVE:
        print(f"   [real_trader] Would breach ${MIN_USDT_RESERVE} reserve — skipping")
        return portfolio, False

    if len(portfolio["holdings"]) >= MAX_OPEN_POSITIONS:
        print(f"   [real_trader] Max {MAX_OPEN_POSITIONS} positions open — skipping")
        return portfolio, False

    print(f"\n   [real_trader] REAL BUY: {symbol} for ${amount_u:.2f} USDT")

    # ── Step 1: get quote ────────────────────────────────────────
    best = _get_best_quote(str(amount_u), contract, symbol)
    if not best:
        print("   [real_trader] Could not get quote — aborting")
        return portfolio, False

    market     = (best.get("market") or {}).get("id", "")
    protocol   = (best.get("market") or {}).get("protocol", "")
    out_amount = best.get("outAmount", "0")
    rec_slip   = (best.get("slippageInfo") or {}).get("recommendSlippage", SLIPPAGE)

    print(f"   Route: {market} | Expected out: {out_amount} {symbol}")

    # ── Steps 2–4: confirm + sign + send ────────────────────────
    key_file = _write_key_tempfile()
    try:
        success = _confirm_and_execute(
            from_amount=str(amount_u),
            from_contract=USDT_SOL, from_symbol="USDT",
            to_contract=contract,   to_symbol=symbol,
            market=market, protocol=protocol,
            recommend_slippage=str(rec_slip),
            out_amount=out_amount,
            key_file=key_file
        )
    finally:
        # Key file is deleted by order_make_sign_send.py, but just in case:
        try:
            Path(key_file).unlink(missing_ok=True)
        except Exception:
            pass

    if not success:
        print(f"   [real_trader] BUY FAILED for {symbol}")
        return portfolio, False

    # ── Update portfolio ─────────────────────────────────────────
    tokens_bought = amount_u / price if price > 0 else 0

    portfolio["usdt_balance"] = max(0, portfolio["usdt_balance"] - amount_u)

    if symbol in portfolio["holdings"]:
        ex    = portfolio["holdings"][symbol]
        total = ex["amount"] + tokens_bought
        cost  = ex["amount"] * ex["buy_price"] + amount_u
        portfolio["holdings"][symbol] = {
            "amount"   : total,
            "buy_price": cost / total,
            "symbol"   : symbol,
            "contract" : contract,
            "bought_at": ex.get("bought_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "mode"     : "REAL",
        }
    else:
        portfolio["holdings"][symbol] = {
            "amount"   : tokens_bought,
            "buy_price": price,
            "symbol"   : symbol,
            "contract" : contract,
            "bought_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode"     : "REAL",
        }

    trade = {
        "type"       : "BUY",
        "symbol"     : symbol,
        "amount_usdt": amount_u,
        "price"      : price,
        "tokens"     : tokens_bought,
        "time"       : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "via"        : "Bitget Wallet Skill API (REAL)",
        "mode"       : "REAL",
    }
    portfolio["trade_history"].append(trade)
    portfolio["total_trades"] = portfolio.get("total_trades", 0) + 1
    save_portfolio(portfolio)

    print(f"\n   ✅ REAL BUY EXECUTED!")
    print(f"   Bought  : {tokens_bought:.6f} {symbol}")
    print(f"   Price   : ${price}")
    print(f"   Spent   : ${amount_u:.4f} USDT")
    print(f"   Balance : ${portfolio['usdt_balance']:.4f} USDT remaining")

    return portfolio, True


def execute_real_sell(portfolio: dict, symbol: str,
                      current_price: float) -> tuple[dict, bool]:
    """
    Execute a real on-chain SELL:
      meme coin → USDT (Solana)
    """
    symbol = symbol.replace("USDT", "")

    if symbol not in portfolio["holdings"]:
        print(f"   [real_trader] Not holding {symbol}")
        return portfolio, False

    holding  = portfolio["holdings"][symbol]
    tokens   = holding["amount"]
    buy_p    = holding["buy_price"]
    contract = holding.get("contract", "")

    if not contract:
        print(f"   [real_trader] No contract for {symbol} — cannot sell on-chain")
        return portfolio, False

    print(f"\n   [real_trader] REAL SELL: {tokens:.6f} {symbol}")

    # ── Step 1: quote ─────────────────────────────────────────────
    best = _get_best_sell_quote(str(tokens), contract, symbol)
    if not best:
        print("   [real_trader] Could not get sell quote — aborting")
        return portfolio, False

    market     = (best.get("market") or {}).get("id", "")
    protocol   = (best.get("market") or {}).get("protocol", "")
    out_amount = best.get("outAmount", "0")
    rec_slip   = (best.get("slippageInfo") or {}).get("recommendSlippage", SLIPPAGE)

    print(f"   Route: {market} | Expected USDT back: {out_amount}")

    # ── Steps 2–4: confirm + sign + send ─────────────────────────
    key_file = _write_key_tempfile()
    try:
        success = _confirm_and_execute(
            from_amount=str(tokens),
            from_contract=contract,  from_symbol=symbol,
            to_contract=USDT_SOL,    to_symbol="USDT",
            market=market, protocol=protocol,
            recommend_slippage=str(rec_slip),
            out_amount=out_amount,
            key_file=key_file
        )
    finally:
        try:
            Path(key_file).unlink(missing_ok=True)
        except Exception:
            pass

    if not success:
        print(f"   [real_trader] SELL FAILED for {symbol}")
        return portfolio, False

    # ── Update portfolio ──────────────────────────────────────────
    sell_value  = tokens * current_price
    profit_loss = sell_value - (tokens * buy_p)
    pct_change  = ((current_price - buy_p) / buy_p) * 100 if buy_p else 0

    portfolio["usdt_balance"] += sell_value
    del portfolio["holdings"][symbol]

    if profit_loss > 0:
        portfolio["winning_trades"] = portfolio.get("winning_trades", 0) + 1

    trade = {
        "type"       : "SELL",
        "symbol"     : symbol,
        "tokens"     : tokens,
        "buy_price"  : buy_p,
        "sell_price" : current_price,
        "sell_value" : sell_value,
        "profit_loss": profit_loss,
        "pct_change" : round(pct_change, 2),
        "time"       : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "via"        : "Bitget Wallet Skill API (REAL)",
        "mode"       : "REAL",
    }
    portfolio["trade_history"].append(trade)
    portfolio["total_trades"] = portfolio.get("total_trades", 0) + 1
    save_portfolio(portfolio)

    result_label = "PROFIT" if profit_loss >= 0 else "LOSS"
    print(f"\n   ✅ REAL SELL EXECUTED!")
    print(f"   Sold    : {tokens:.6f} {symbol}")
    print(f"   Result  : {result_label} ${abs(profit_loss):.4f} ({pct_change:.2f}%)")
    print(f"   Balance : ${portfolio['usdt_balance']:.4f} USDT")

    return portfolio, True


def execute_partial_sell(portfolio: dict, symbol: str,
                         fraction: float,
                         current_price: float) -> tuple[dict, bool]:
    """Partial sell (fraction of holdings). Adjusts token amount then calls full sell."""
    symbol_clean = symbol.replace("USDT", "")
    if symbol_clean not in portfolio["holdings"]:
        return portfolio, False

    h         = portfolio["holdings"][symbol_clean]
    orig_amt  = h["amount"]
    sell_amt  = orig_amt * fraction
    keep_amt  = orig_amt - sell_amt

    # Temporarily set amount to what we're selling
    portfolio["holdings"][symbol_clean]["amount"] = sell_amt
    portfolio, ok = execute_real_sell(portfolio, symbol_clean, current_price)

    if ok and keep_amt > 0:
        # Restore remaining tokens (sell func deleted the key)
        portfolio["holdings"][symbol_clean] = {
            **h,
            "amount"   : keep_amt,
            "half_sold": True,
        }
        save_portfolio(portfolio)

    return portfolio, ok
