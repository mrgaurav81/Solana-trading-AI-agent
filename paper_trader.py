import json
import os
from datetime import datetime
from market_scanner import run_scanner
from ai_brain import run_ai_brain
from bitget_skill import get_swap_quote


PORTFOLIO_FILE   = "portfolio.json"
SETTINGS_FILE    = "agent_settings.json"
STARTING_BALANCE = 100.0


def _load_sl_tp_settings():
    """Load stop-loss / take-profit % from settings file."""
    defaults = {"stop_loss_pct": 5.0, "take_profit_pct": 15.0}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
                defaults.update(saved)
        except:
            pass
    return defaults["stop_loss_pct"], defaults["take_profit_pct"]


def load_portfolio():
    """
    Loads portfolio from file or creates fresh one.
    """
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, "r") as f:
            return json.load(f)
    else:
        portfolio = {
            "usdt_balance"  : STARTING_BALANCE,
            "holdings"      : {},
            "trade_history" : [],
            "total_trades"  : 0,
            "winning_trades": 0,
            "created_at"    : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_portfolio(portfolio)
        return portfolio

def check_stop_loss_take_profit(portfolio, current_prices):
    """
    Checks all holdings against stop-loss and tiered take-profit levels.

    Thresholds read from agent_settings.json:
      Stop-loss       : sell 100% if price drops  SL%   (default -5%)
      Take-profit T1  : sell 50%  if price rises  TP%   (default +15%)
      Take-profit T2  : sell 100% if price rises  TP*2% (default +30%)

    Returns list of sell-action dicts with an optional 'fraction' key
    (1.0 = full sell, 0.5 = partial sell of half the position).
    """
    STOP_LOSS_PCT, TAKE_PROFIT_PCT = _load_sl_tp_settings()
    TP2_PCT = TAKE_PROFIT_PCT * 2   # second tier

    tokens_to_sell = []

    for symbol, holding in portfolio["holdings"].items():
        buy_price     = holding.get("buy_price", 0)
        current_price = current_prices.get(symbol, 0)
        half_sold     = holding.get("half_sold", False)  # track partial exits

        if buy_price == 0 or current_price == 0:
            continue

        pct_change = ((current_price - buy_price) / buy_price) * 100

        if pct_change <= -STOP_LOSS_PCT:
            tokens_to_sell.append({
                "symbol"       : symbol,
                "reason"       : "STOP_LOSS",
                "pct_change"   : round(pct_change, 2),
                "buy_price"    : buy_price,
                "current_price": current_price,
                "fraction"     : 1.0
            })
            print(f"   STOP LOSS triggered for {symbol}! "
                  f"Down {abs(pct_change):.2f}%")

        elif pct_change >= TP2_PCT:
            # Second tier — sell whatever is left
            tokens_to_sell.append({
                "symbol"       : symbol,
                "reason"       : "TAKE_PROFIT_T2",
                "pct_change"   : round(pct_change, 2),
                "buy_price"    : buy_price,
                "current_price": current_price,
                "fraction"     : 1.0
            })
            print(f"   TAKE PROFIT T2 (+{TP2_PCT:.0f}%) for {symbol}! "
                  f"Up {pct_change:.2f}%")

        elif pct_change >= TAKE_PROFIT_PCT and not half_sold:
            # First tier — partial sell (50%)
            tokens_to_sell.append({
                "symbol"       : symbol,
                "reason"       : "TAKE_PROFIT_T1",
                "pct_change"   : round(pct_change, 2),
                "buy_price"    : buy_price,
                "current_price": current_price,
                "fraction"     : 0.5
            })
            print(f"   TAKE PROFIT T1 (+{TAKE_PROFIT_PCT:.0f}%) for {symbol}! "
                  f"Selling 50% — Up {pct_change:.2f}%")

    return tokens_to_sell


def run_stop_loss_check(portfolio, current_prices):
    """
    Runs tiered stop-loss / take-profit check on all holdings.
    Uses execute_partial_sell for T1 (50%) and execute_paper_sell for full exits.
    """
    print("\nRunning stop-loss / take-profit check...")

    tokens_to_sell = check_stop_loss_take_profit(portfolio, current_prices)

    if not tokens_to_sell:
        print("   All positions within safe range.")
        return portfolio, []

    sold_tokens = []
    for item in tokens_to_sell:
        symbol        = item["symbol"]
        current_price = item["current_price"]
        reason        = item["reason"]
        pct_change    = item["pct_change"]
        fraction      = item.get("fraction", 1.0)

        print(f"\n   Auto-selling {symbol} — {reason} ({pct_change}%) fraction={fraction}")

        if fraction < 1.0:
            portfolio, success = execute_partial_sell(
                portfolio, symbol, fraction, current_price
            )
        else:
            portfolio, success = execute_paper_sell(
                portfolio, symbol, current_price
            )

        if success:
            sold_tokens.append(item)

    return portfolio, sold_tokens





def save_portfolio(portfolio):
    """
    Saves portfolio to file.
    """
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)


def get_bitget_swap_quote(token, amount_usdt):
    """
    Gets real swap quote from Bitget Wallet Skill API.
    Shows exact amount of tokens we'll receive.
    Uses 110+ DEX aggregation for best price.
    """
    symbol   = token.get("symbol", "")
    contract = token.get("contract", "")

    if not contract:
        print(f"   No contract address for {symbol} — using market price")
        return None

    print(f"   Getting Bitget swap quote for {symbol}...")

    quote = get_swap_quote(
        from_contract="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        from_symbol="USDC",
        from_amount=str(amount_usdt),
        to_contract=contract,
        to_symbol=symbol
    )

    if "error" not in str(quote):
        print(f"   Swap quote received from Bitget!")
        return quote

    return None


def calculate_portfolio_value(portfolio, current_prices):
    """
    Calculates total portfolio value.
    """
    total = portfolio["usdt_balance"]

    for symbol, holding in portfolio["holdings"].items():
        if symbol in current_prices:
            value  = holding["amount"] * current_prices[symbol]
            total += value

    return total


def execute_paper_buy(portfolio, token, amount_usdt, quote=None):
    """
    Simulates buying a token.
    Shows swap quote from Bitget if available.
    """
    symbol      = token.get("symbol", "").replace("USDT", "")
    price       = float(token.get("price", 0))
    amount_usdt = float(amount_usdt)

    if portfolio["usdt_balance"] < amount_usdt:
        print(f"   Not enough balance! Have ${portfolio['usdt_balance']:.2f}")
        return portfolio, False

    tokens_bought = amount_usdt / price

    # Show swap quote details if available
    if quote:
        print(f"   Bitget Swap Quote Details:")
        print(f"   Best route found via 110+ DEX aggregation")

    portfolio["usdt_balance"] -= amount_usdt

    contract    = token.get("contract", "")
    bought_at   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if symbol in portfolio["holdings"]:
        existing   = portfolio["holdings"][symbol]
        total_amt  = existing["amount"] + tokens_bought
        total_cost = (existing["amount"] * existing["buy_price"]) + amount_usdt
        avg_price  = total_cost / total_amt

        portfolio["holdings"][symbol] = {
            "amount"    : total_amt,
            "buy_price" : avg_price,
            "symbol"    : symbol,
            "contract"  : contract or existing.get("contract", ""),
            "bought_at" : existing.get("bought_at", bought_at)
        }
    else:
        portfolio["holdings"][symbol] = {
            "amount"    : tokens_bought,
            "buy_price" : price,
            "symbol"    : symbol,
            "contract"  : contract,
            "bought_at" : bought_at
        }

    trade = {
        "type"       : "BUY",
        "symbol"     : symbol,
        "amount_usdt": amount_usdt,
        "price"      : price,
        "tokens"     : tokens_bought,
        "time"       : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "via"        : "Bitget Wallet Skill API (paper)"
    }
    portfolio["trade_history"].append(trade)
    portfolio["total_trades"] += 1

    save_portfolio(portfolio)

    print(f"\n   PAPER BUY EXECUTED!")
    print(f"   Bought  : {tokens_bought:.6f} {symbol}")
    print(f"   Price   : ${price}")
    print(f"   Spent   : ${amount_usdt}")
    print(f"   Balance : ${portfolio['usdt_balance']:.2f} USDT remaining")
    print(f"   Via     : Bitget Wallet Skill API")

    return portfolio, True


def execute_partial_sell(portfolio, symbol, fraction, current_price):
    """
    Sells a fraction (0.0 – 1.0) of a holding.
    Used for tiered take-profit: sell 50% at T1, sell rest at T2.
    Sets holding['half_sold'] = True after first partial exit so T1 never fires twice.
    """
    symbol = symbol.replace("USDT", "")

    if symbol not in portfolio["holdings"]:
        print(f"   Not holding {symbol}")
        return portfolio, False

    holding     = portfolio["holdings"][symbol]
    tokens      = holding["amount"]
    buy_price   = holding["buy_price"]
    sell_tokens = tokens * fraction
    sell_value  = sell_tokens * current_price
    profit_loss = sell_value - (sell_tokens * buy_price)

    portfolio["usdt_balance"] += sell_value

    remaining = tokens - sell_tokens
    if remaining > 0:
        portfolio["holdings"][symbol]["amount"]    = remaining
        portfolio["holdings"][symbol]["half_sold"] = True
    else:
        del portfolio["holdings"][symbol]

    if profit_loss > 0:
        portfolio["winning_trades"] = portfolio.get("winning_trades", 0) + 1

    trade = {
        "type"       : "SELL_PARTIAL",
        "symbol"     : symbol,
        "tokens"     : sell_tokens,
        "fraction"   : fraction,
        "buy_price"  : buy_price,
        "sell_price" : current_price,
        "sell_value" : sell_value,
        "profit_loss": profit_loss,
        "time"       : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    portfolio["trade_history"].append(trade)
    portfolio["total_trades"] = portfolio.get("total_trades", 0) + 1

    save_portfolio(portfolio)

    result = "PROFIT" if profit_loss > 0 else "LOSS"
    print(f"\n   PARTIAL SELL EXECUTED ({fraction*100:.0f}%)!")
    print(f"   Sold    : {sell_tokens:.6f} {symbol}")
    print(f"   Result  : {result} ${abs(profit_loss):.2f}")
    print(f"   Remaining: {remaining:.6f} tokens")
    print(f"   Balance : ${portfolio['usdt_balance']:.2f} USDT")

    return portfolio, True


def execute_paper_sell(portfolio, symbol, current_price):
    """
    Simulates selling a token and calculates profit/loss.
    """
    symbol = symbol.replace("USDT", "")

    if symbol not in portfolio["holdings"]:
        print(f"   Not holding {symbol}")
        return portfolio, False

    holding     = portfolio["holdings"][symbol]
    tokens      = holding["amount"]
    buy_price   = holding["buy_price"]
    sell_value  = tokens * current_price
    profit_loss = sell_value - (tokens * buy_price)
    pct_change  = ((current_price - buy_price) / buy_price) * 100

    portfolio["usdt_balance"] += sell_value
    del portfolio["holdings"][symbol]

    if profit_loss > 0:
        portfolio["winning_trades"] += 1

    trade = {
        "type"       : "SELL",
        "symbol"     : symbol,
        "tokens"     : tokens,
        "buy_price"  : buy_price,
        "sell_price" : current_price,
        "sell_value" : sell_value,
        "profit_loss": profit_loss,
        "pct_change" : pct_change,
        "time"       : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    portfolio["trade_history"].append(trade)
    portfolio["total_trades"] += 1

    save_portfolio(portfolio)

    result = "PROFIT" if profit_loss > 0 else "LOSS"
    print(f"\n   PAPER SELL EXECUTED!")
    print(f"   Sold    : {tokens:.6f} {symbol}")
    print(f"   Result  : {result} ${abs(profit_loss):.2f} ({pct_change:.2f}%)")
    print(f"   Balance : ${portfolio['usdt_balance']:.2f} USDT")

    return portfolio, True


def print_portfolio_status(portfolio, current_prices):
    """
    Prints full portfolio summary.
    """
    total_value = calculate_portfolio_value(portfolio, current_prices)
    profit_loss = total_value - STARTING_BALANCE
    pct_change  = ((total_value - STARTING_BALANCE) / STARTING_BALANCE) * 100

    print("\n" + "=" * 60)
    print("  PORTFOLIO STATUS")
    print("=" * 60)
    print(f"\n   USDT Balance : ${portfolio['usdt_balance']:.2f}")
    print(f"   Total Value  : ${total_value:.2f}")
    print(f"   Started with : ${STARTING_BALANCE:.2f}")

    if profit_loss >= 0:
        print(f"   Total P&L    : +${profit_loss:.2f} (+{pct_change:.2f}%)")
    else:
        print(f"   Total P&L    : -${abs(profit_loss):.2f} ({pct_change:.2f}%)")

    print(f"   Total Trades : {portfolio['total_trades']}")

    if portfolio["holdings"]:
        print(f"\n   Current Holdings:")
        for symbol, holding in portfolio["holdings"].items():
            price = current_prices.get(symbol, holding["buy_price"])
            value = holding["amount"] * price
            pnl   = value - (holding["amount"] * holding["buy_price"])
            print(f"   {symbol}: {holding['amount']:.4f} tokens | "
                  f"Bought @ ${holding['buy_price']:.6f} | "
                  f"P&L: ${pnl:.2f}")
    else:
        print("\n   No open positions.")

    if portfolio["trade_history"]:
        print(f"\n   Last 3 Trades:")
        for trade in portfolio["trade_history"][-3:]:
            if trade["type"] == "BUY":
                print(f"   BUY  {trade['symbol']} | "
                      f"${trade['amount_usdt']} | "
                      f"@ ${trade['price']} | "
                      f"{trade['time']}")
            else:
                print(f"   SELL {trade['symbol']} | "
                      f"P&L ${trade['profit_loss']:.2f} | "
                      f"{trade['time']}")

    print("\n" + "=" * 60)


def run_paper_trader():
    """
    Full paper trading pipeline:
    1. Load portfolio
    2. Get AI decisions (with security checks)
    3. Get Bitget swap quotes
    4. Execute paper trades
    5. Show portfolio status
    """

    # Step 1 - load portfolio
    portfolio = load_portfolio()
    print(f"\nPortfolio loaded. Balance: ${portfolio['usdt_balance']:.2f} USDT")

    # Step 2 - get AI decisions
    print("\nStep 2 - Getting AI decisions...")
    decisions, best_pick = run_ai_brain()

    if not decisions:
        print("No decisions from AI. Exiting.")
        return

    # Build current prices lookup
    bullish_tokens = run_scanner()
    current_prices = {}
    token_lookup   = {}

    for token in bullish_tokens:
        symbol                = token.get("symbol", "")
        current_prices[symbol] = float(token.get("price", 0))
        token_lookup[symbol]   = token

    # Step 3 + 4 - get quotes and execute trades
    print("\nStep 3 - Getting Bitget swap quotes and executing trades...")
    print("-" * 60)

    for decision in decisions:

        if "best_pick" in decision:
            continue

        symbol     = decision.get("token", "")
        action     = decision.get("decision", "SKIP")
        amount     = decision.get("amount", "5")
        confidence = decision.get("confidence", "LOW")

        print(f"\nProcessing {symbol}...")
        print(f"   Decision   : {action}")
        print(f"   Confidence : {confidence}")

        if action == "BUY" and confidence == "HIGH":

            token_data = token_lookup.get(symbol)

            if not token_data:
                print(f"   No data for {symbol} — skipping")
                continue

            # Get Bitget swap quote
            quote = get_bitget_swap_quote(token_data, amount)

            # Execute paper trade
            portfolio, success = execute_paper_buy(
                portfolio,
                token_data,
                amount,
                quote
            )

        elif action == "SELL":
            price = current_prices.get(symbol, 0)
            portfolio, success = execute_paper_sell(
                portfolio, symbol, price
            )

        else:
            print(f"   Skipping — {action} with {confidence} confidence")

    # Step 5 - show portfolio
    print_portfolio_status(portfolio, current_prices)


if __name__ == "__main__":
    print("=" * 60)
    print("  Solana AI Trading Agent - Paper Trader")
    print("=" * 60)
    run_paper_trader()