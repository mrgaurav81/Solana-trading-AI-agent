import time
import schedule
import json
import os
import requests
import random
import threading
from datetime import datetime, timedelta
from market_scanner import run_scanner
from ai_brain import run_ai_brain
from real_trader import (
    load_portfolio, save_portfolio,
    execute_real_buy  as execute_paper_buy,
    execute_real_sell as execute_paper_sell,
    execute_partial_sell,
    print_portfolio_status, calculate_portfolio_value,
    run_stop_loss_check,
    sync_balance_from_chain
)
from price_fetcher import get_token_price
from agent_control import (
    get_control, is_running, get_mode,
    start_agent as ctrl_start,
    stop_agent as ctrl_stop,
    add_pending_trade, wait_for_decision
)
from telegram_bot import (
    start_bot,
    notify_agent_started, notify_scan_complete,
    notify_ai_decision, notify_trade_executed,
    notify_portfolio_status, notify_stop_loss,
    notify_error, notify_trade_confirmation,
    notify_recovery_mode
)

SCAN_INTERVAL_MINUTES = 15
STARTING_BALANCE      = 8.70   # real USDT on Solana wallet
LOG_FILE              = "agent_log.txt"
SETTINGS_FILE         = "agent_settings.json"

# Rate-limit portfolio Telegram updates to once per hour
_last_portfolio_notify = datetime.min

# Recovery mode state — track so we only alert once per activation
_recovery_active = False


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg  = f"[{timestamp}] {message}"
    print(full_msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(full_msg + "\n")
    except:
        pass


def load_settings():
    defaults = {
        "stop_loss_pct"      : 5.0,
        "take_profit_pct"    : 15.0,
        "scan_interval_mins" : 15,
        "max_trade_amount"   : 10.0
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
                defaults.update(saved)
        except:
            pass
    return defaults


def get_current_prices(holdings):
    """
    Fetches live prices for all holdings using the unified price_fetcher.
    Passes contract address for meme coins that aren't on Bitget spot.

    SAFETY GUARD: if the resolved price is more than 10x away from the
    buy price, it almost certainly comes from a different token that shares
    the same ticker symbol (e.g. Solana meme PIXEL vs listed PIXEL on
    Bitget spot). We discard that price and skip the holding so the agent
    never triggers a fake 3000% take-profit.
    """
    prices = {}
    for symbol, h in holdings.items():
        contract  = h.get("contract", "")
        buy_price = h.get("buy_price", 0)
        price, source = get_token_price(symbol, contract)
        if price and price > 0:
            # Sanity check: reject prices that are >10x above or <10x below buy price
            if buy_price > 0:
                ratio = price / buy_price
                if ratio > 10 or ratio < 0.1:
                    log(f"   ⚠️  PRICE SANITY FAIL for {symbol}: "
                        f"buy=${buy_price:.8f}, fetched=${price:.8f} "
                        f"(ratio={ratio:.1f}x via {source}) — "
                        f"likely wrong-token collision, skipping")
                    continue   # do NOT use this price
            prices[symbol] = price
            log(f"   {symbol} price: ${price} (via {source})")
        else:
            log(f"   Could not get price for {symbol}")
    return prices


def is_internet_available():
    try:
        requests.get("https://api.bitget.com", timeout=5)
        return True
    except:
        return False


def wait_for_internet():
    log("Internet lost. Waiting for reconnection...")
    attempts = 0
    while True:
        time.sleep(60)
        attempts += 1
        if is_internet_available():
            log(f"Internet reconnected after {attempts} minutes!")
            return
        log(f"Still no internet... attempt {attempts}")


def check_portfolio_recovery(portfolio, current_prices):
    """
    Returns True if agent is in recovery mode (portfolio < $70).
    Sends a Telegram alert on first activation and on exit.
    Resumes normal trading once value recovers above $80.
    """
    global _recovery_active

    total_value = calculate_portfolio_value(portfolio, current_prices)

    # Recovery thresholds scaled for real $8.70 balance:
    # Enter recovery below $4.00, exit above $5.00
    if total_value < 4.00:
        if not _recovery_active:
            _recovery_active = True
            log("⚠️  RECOVERY MODE ACTIVATED — portfolio below $4.00")
            try:
                notify_recovery_mode(
                    active=True,
                    total_value=total_value
                )
            except:
                pass
        return True  # In recovery, block buys

    if _recovery_active and total_value >= 5.00:
        _recovery_active = False
        log("✅  Recovery mode deactivated — portfolio back above $5.00")
        try:
            notify_recovery_mode(
                active=False,
                total_value=total_value
            )
        except:
            pass

    return False


def run_smarter_sell_check(portfolio, current_prices, bullish_tokens):
    """
    Additional sell signals beyond basic stop-loss / take-profit:
    1. Take-profit hit       → sell 100% immediately (reads from agent_settings.json)
    2. Held >24h with no profit  → sell
    3. Momentum reversal        → sell if token NOT in this cycle's bullish list
    """
    settings        = load_settings()
    tp_pct          = settings.get("take_profit_pct", 15.0)
    bullish_symbols = {t.get("symbol", "") for t in bullish_tokens}
    sold_tokens     = []

    for symbol, h in list(portfolio["holdings"].items()):
        buy_price     = h.get("buy_price", 0)
        current_price = current_prices.get(symbol, 0)
        bought_at     = h.get("bought_at", "")
        if not buy_price or not current_price:
            continue

        pct_change = ((current_price - buy_price) / buy_price) * 100

        # Signal 1: Take-profit hit — sell 100%
        if pct_change >= tp_pct:
            log(f"   TAKE PROFIT hit for {symbol} (+{pct_change:.2f}% >= +{tp_pct:.0f}%) — selling 100%")
            portfolio, success = execute_paper_sell(portfolio, symbol, current_price)
            if success:
                sold_tokens.append({
                    "symbol"       : symbol,
                    "reason"       : "TAKE_PROFIT",
                    "pct_change"   : round(pct_change, 2),
                    "buy_price"    : buy_price,
                    "current_price": current_price
                })
            continue

        # Signal 2: held > 24 hours with zero or negative profit
        if bought_at:
            try:
                held_since = datetime.strptime(bought_at, "%Y-%m-%d %H:%M:%S")
                held_hours = (datetime.now() - held_since).total_seconds() / 3600
                if held_hours > 24 and pct_change <= 0:
                    log(f"   Selling {symbol} — held {held_hours:.1f}h with no profit ({pct_change:.1f}%)")
                    portfolio, success = execute_paper_sell(portfolio, symbol, current_price)
                    if success:
                        sold_tokens.append({
                            "symbol"       : symbol,
                            "reason"       : "HELD_24H_NO_PROFIT",
                            "pct_change"   : round(pct_change, 2),
                            "buy_price"    : buy_price,
                            "current_price": current_price
                        })
                    continue
            except:
                pass

        # Signal 3: momentum reversal — token not bullish this cycle AND in loss
        if symbol not in bullish_symbols and pct_change < 0:
            log(f"   Selling {symbol} — momentum reversed, not in bullish list")
            portfolio, success = execute_paper_sell(portfolio, symbol, current_price)
            if success:
                sold_tokens.append({
                    "symbol"       : symbol,
                    "reason"       : "MOMENTUM_REVERSAL",
                    "pct_change"   : round(pct_change, 2),
                    "buy_price"    : buy_price,
                    "current_price": current_price
                })

    return portfolio, sold_tokens


def run_trading_cycle():
    """
    Full trading cycle — only runs if agent is set to running.
    """
    global _last_portfolio_notify

    if not is_running():
        log("Agent is stopped. Skipping cycle.")
        return

    log("=" * 50)
    log("Starting new trading cycle...")
    log("=" * 50)

    if not is_internet_available():
        wait_for_internet()

    settings = load_settings()

    try:
        # Step 1 — load portfolio and sync real on-chain balance
        portfolio = load_portfolio()
        portfolio = sync_balance_from_chain(portfolio)
        log(f"Portfolio loaded. Real USDT Balance: ${portfolio['usdt_balance']:.4f}")

        # Step 2 — stop-loss / take-profit check (uses price_fetcher via paper_trader)
        if portfolio["holdings"]:
            log("Checking stop-loss and take-profit levels...")
            quick_prices = get_current_prices(portfolio["holdings"])

            if quick_prices:
                portfolio, sold = run_stop_loss_check(portfolio, quick_prices)
                for item in sold:
                    try:
                        notify_stop_loss(
                            symbol     = item["symbol"],
                            reason     = item["reason"],
                            pct_change = item["pct_change"],
                            buy_price  = item["buy_price"],
                            sell_price = item["current_price"],
                            balance    = portfolio["usdt_balance"]
                        )
                    except:
                        pass
                    log(f"{item['reason']} triggered for "
                        f"{item['symbol']} at {item['pct_change']}%")

        # Step 3 — scan market
        log("Scanning market via Bitget Wallet Skill Hotpicks...")
        bullish_tokens = run_scanner()

        if bullish_tokens:
            log(f"Found {len(bullish_tokens)} bullish tokens — proceeding to AI analysis")

        if not bullish_tokens:
            log("No bullish tokens found this cycle.")
            return

        log(f"Found {len(bullish_tokens)} bullish tokens")

        # Step 4 — smarter sell check (momentum reversal, 24h hold)
        if portfolio["holdings"]:
            quick_prices = get_current_prices(portfolio["holdings"])
            if quick_prices:
                portfolio, extra_sold = run_smarter_sell_check(
                    portfolio, quick_prices, bullish_tokens
                )
                for item in extra_sold:
                    try:
                        notify_stop_loss(
                            symbol     = item["symbol"],
                            reason     = item["reason"],
                            pct_change = item["pct_change"],
                            buy_price  = item["buy_price"],
                            sell_price = item["current_price"],
                            balance    = portfolio["usdt_balance"]
                        )
                    except:
                        pass

        # Step 5 — AI decisions
        log("Getting AI decisions with security filtering...")
        decisions, best_pick = run_ai_brain()

        if not decisions:
            log("AI returned no decisions.")
            return

        # Build lookups
        current_prices = {}
        token_lookup   = {}
        for token in bullish_tokens:
            symbol                 = token.get("symbol", "")
            current_prices[symbol] = float(token.get("price", 0))
            token_lookup[symbol]   = token

        # Step 6 — Portfolio recovery mode check
        in_recovery = check_portfolio_recovery(portfolio, current_prices)
        if in_recovery:
            log("Recovery mode active — skipping all BUY decisions")

        # Step 7 — process decisions
        trades_made = 0
        mode        = get_mode()
        log(f"Trading mode: {mode.upper()}")

        for decision in decisions:

            if "best_pick" in decision:
                continue
            if "market_summary" in decision:
                continue

            if not is_running():
                log("Agent stopped mid-cycle. Exiting.")
                return

            symbol     = decision.get("token",      "")
            action     = decision.get("decision",   "SKIP")
            confidence = decision.get("confidence", "LOW")
            reason     = decision.get("reason",     "")
            strategy   = decision.get("strategy",   "")

            # Cap AI-suggested amount to user's max_trade_amount setting
            ai_amount  = float(decision.get("amount", 5))
            max_amount = float(settings.get("max_trade_amount", 10.0))

            # Tiered position sizing by confidence:
            #   HIGH   → full max_trade_amount (best signals, full conviction)
            #   MEDIUM → 50% of max_trade_amount (promising but cautious)
            #   LOW    → skip entirely
            if confidence == "HIGH":
                amount = str(min(ai_amount, max_amount))
            elif confidence == "MEDIUM":
                amount = str(min(ai_amount, max_amount * 0.5))
            else:
                amount = None   # LOW → skip

            log(f"Processing {symbol}: {action} ({confidence}) [{strategy}]")

            if action == "BUY" and amount is not None:

                # Skip in recovery mode
                if in_recovery:
                    log(f"Recovery mode — skipping BUY of {symbol}")
                    continue

                symbol_clean = symbol.replace("USDT", "")
                if symbol_clean in portfolio["holdings"]:
                    log(f"Already holding {symbol} — skipping")
                    continue

                if portfolio["usdt_balance"] < 3.00:
                    log("Balance too low — protecting $3 reserve")
                    continue

                token_data = token_lookup.get(symbol)
                if not token_data:
                    log(f"No data for {symbol} — skipping")
                    continue

                # Blacklist check — skip tokens blocked in agent_settings.json
                blacklist = [s.upper() for s in settings.get("blacklisted_tokens", [])]
                if symbol.upper().replace("USDT", "") in blacklist:
                    log(f"   {symbol} is blacklisted — skipping BUY")
                    continue

                # Quote handled internally by real_trader execute_real_buy
                quote    = None
                contract = token_data.get("contract", "")

                # Confirm trade
                if mode == "auto":
                    confirmed = True
                    log(f"Auto mode — executing {symbol} trade")
                else:
                    trade_id = f"trade_{symbol}_{random.randint(1000,9999)}"
                    add_pending_trade(
                        trade_id   = trade_id,
                        symbol     = symbol,
                        decision   = action,
                        confidence = confidence,
                        reason     = reason,
                        amount     = amount,
                        strategy   = strategy
                    )
                    try:
                        notify_trade_confirmation(
                            trade_id   = trade_id,
                            symbol     = symbol,
                            decision   = action,
                            confidence = confidence,
                            reason     = reason,
                            amount     = amount,
                            strategy   = strategy
                        )
                    except:
                        pass
                    log("Waiting for confirmation (Dashboard or Telegram)...")
                    confirmed = wait_for_decision(trade_id, timeout=120)

                if not confirmed:
                    log(f"Trade skipped for {symbol}")
                    continue

                # Execute buy — store bought_at timestamp and contract for later price checks
                portfolio, success = execute_paper_buy(
                    portfolio, token_data, amount, quote
                )
                if success:
                    # Persist timestamp and contract so smarter-sell logic can use them
                    if symbol_clean in portfolio["holdings"]:
                        portfolio["holdings"][symbol_clean]["bought_at"] = \
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        portfolio["holdings"][symbol_clean]["contract"] = contract
                    save_portfolio(portfolio)

                    trades_made += 1
                    price  = float(token_data.get("price", 0))
                    tokens = float(amount) / price if price > 0 else 0
                    try:
                        notify_trade_executed(
                            trade_type ="BUY",
                            symbol     =symbol,
                            amount_usdt=float(amount),
                            price      =price,
                            tokens     =tokens,
                            balance    =portfolio["usdt_balance"]
                        )
                    except:
                        pass
                    log(f"BUY executed: {symbol} for ${amount}")

            elif action == "SELL":
                # Prefer price from scanner; fall back to live price_fetcher
                price = current_prices.get(symbol, 0)
                if price == 0:
                    symbol_clean = symbol.replace("USDT", "")
                    contract     = portfolio["holdings"].get(symbol_clean, {}).get("contract", "")
                    fetched, _   = get_token_price(symbol_clean, contract)
                    price        = fetched or 0

                if price == 0:
                    log(f"SELL skipped for {symbol} — could not get live price")
                else:
                    portfolio, success = execute_paper_sell(
                        portfolio, symbol, price
                    )
                    if success:
                        trades_made += 1
                        holding      = portfolio["holdings"].get(symbol.replace("USDT",""), {})
                        sell_tokens  = holding.get("amount", 0) if holding else 0
                        try:
                            notify_trade_executed(
                                trade_type ="SELL",
                                symbol     =symbol,
                                amount_usdt=round(sell_tokens * price, 2),
                                price      =price,
                                tokens     =sell_tokens,
                                balance    =portfolio["usdt_balance"]
                            )
                        except:
                            pass

            else:
                log(f"Skipping {symbol} — {action} ({confidence})")

        # Step 8 — portfolio summary (rate-limited to once per hour)
        total_value = calculate_portfolio_value(portfolio, current_prices)
        profit_loss = total_value - STARTING_BALANCE

        now = datetime.now()
        if (now - _last_portfolio_notify).total_seconds() >= 3600:
            try:
                notify_portfolio_status(
                    balance     =portfolio["usdt_balance"],
                    total_value =total_value,
                    profit_loss =profit_loss,
                    total_trades=portfolio["total_trades"]
                )
                _last_portfolio_notify = now
            except:
                pass

        print_portfolio_status(portfolio, current_prices)
        log(f"Cycle complete. Trades made: {trades_made}")
        log(f"Next scan in {SCAN_INTERVAL_MINUTES} minutes...")

    except Exception as e:
        error_msg = str(e)
        log(f"ERROR in trading cycle: {error_msg}")
        try:
            notify_error(error_msg)
        except:
            pass

def run_stop_loss_monitor():
    """
    Lightweight background monitor — runs every 60 seconds.
    Checks stop-loss and take-profit on ALL holdings independently
    of the main 15-minute trading cycle, so positions close within
    ~1 minute of hitting their threshold.
    """
    log("Stop-loss monitor started (checks every 60s)")
    while True:
        try:
            time.sleep(60)  # wait first, then check

            if not is_running():
                continue

            portfolio = load_portfolio()
            if not portfolio["holdings"]:
                continue

            prices = get_current_prices(portfolio["holdings"])
            if not prices:
                continue

            portfolio, sold = run_stop_loss_check(portfolio, prices)

            for item in sold:
                log(f"[MONITOR] {item['reason']} → {item['symbol']} "
                    f"at {item['pct_change']}%")
                try:
                    notify_stop_loss(
                        symbol     = item["symbol"],
                        reason     = item["reason"],
                        pct_change = item["pct_change"],
                        buy_price  = item["buy_price"],
                        sell_price = item["current_price"],
                        balance    = portfolio["usdt_balance"]
                    )
                except:
                    pass

        except Exception as e:
            log(f"Stop-loss monitor error: {e}")



def liquidate_all_holdings():
    """
    Sells all active holdings at current market price.
    Usually triggered when the agent is stopped or paused.
    """
    log("LIQUIDATION TRIGGERED: Agent stopped. Selling all holdings.")
    portfolio = load_portfolio()
    
    if not portfolio["holdings"]:
        log("Portfolio is already empty.")
        return

    prices = get_current_prices(portfolio["holdings"])
    
    for symbol, h in list(portfolio["holdings"].items()):
        price = prices.get(symbol, 0)
        tokens = h.get("amount", 0)
        
        # Fallback if price wasn't fetched in batch
        if price == 0:
            contract = h.get("contract", "")
            fetched_price, _ = get_token_price(symbol, contract)
            price = fetched_price or 0

        if price > 0:
            log(f"   Liquidating {symbol}: {tokens} tokens at ${price}")
            portfolio, success = execute_paper_sell(portfolio, symbol, price)
            if success:
                try:
                    notify_trade_executed(
                        trade_type ="SELL (LIQUIDATION)",
                        symbol     =symbol,
                        amount_usdt=round(tokens * price, 2),
                        price      =price,
                        tokens     =tokens,
                        balance    =portfolio["usdt_balance"]
                    )
                except:
                    pass
        else:
            log(f"   ERROR: Could not get price to liquidate {symbol}")


def start_agent():
    """
    Main agent loop with auto-reconnect and control system.
    """
    print("=" * 60)
    print("  Solana AI Trading Agent - REAL TRADING MODE 🔥")
    print("  Wallet: Gwj7WyRfoNP1a5ZcUhEDfNKmtRxGYPffKCz97TpRapmT")
    print("  Chain : Solana | Gas: no_gas mode (deducted from USDT)")
    print("=" * 60)
    print(f"\nStarting: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Interval: {SCAN_INTERVAL_MINUTES} minutes")
    print("Press Ctrl+C to stop\n")

    log("Starting Telegram bot...")
    start_bot()

    # Start the dedicated stop-loss monitor thread (checks every 60s)
    monitor_thread = threading.Thread(
        target=run_stop_loss_monitor, daemon=True
    )
    monitor_thread.start()
    log("Stop-loss monitor running (60s interval)")

    ctrl_start()
    log("Agent started successfully!")

    try:
        notify_agent_started()
    except:
        pass

    while True:
        try:
            log("Running first cycle now...")
            run_trading_cycle()

            settings = load_settings()
            interval = settings.get("scan_interval_mins", SCAN_INTERVAL_MINUTES)

            schedule.clear()
            schedule.every(interval).minutes.do(run_trading_cycle)

            while True:
                if not is_running():
                    log("Agent stopped externally. Waiting...")
                    liquidate_all_holdings()
                    schedule.clear()
                    while not is_running():
                        time.sleep(5)
                    log("Agent restarted! Running cycle...")
                    run_trading_cycle()

                    settings = load_settings()
                    interval = settings.get("scan_interval_mins", SCAN_INTERVAL_MINUTES)
                    schedule.every(interval).minutes.do(run_trading_cycle)

                # Live reschedule if the user changed the scan interval
                new_settings = load_settings()
                new_interval = new_settings.get("scan_interval_mins", SCAN_INTERVAL_MINUTES)
                if new_interval != interval:
                    log(f"Scan interval changed: {interval}m → {new_interval}m — rescheduling")
                    interval = new_interval
                    schedule.clear()
                    schedule.every(interval).minutes.do(run_trading_cycle)

                schedule.run_pending()
                time.sleep(10)

        except KeyboardInterrupt:
            log("Agent stopped by user.")
            ctrl_stop()
            break

        except Exception as e:
            log(f"Agent crashed: {e}")
            log("Waiting 60 seconds then restarting...")
            time.sleep(60)
            log("Restarting...")
            schedule.clear()


if __name__ == "__main__":
    start_agent()