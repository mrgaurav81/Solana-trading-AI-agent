from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
import requests
import threading
import subprocess
import sys
import time
from datetime import datetime
from agent_control import (
    get_control, start_agent as ctrl_start,
    stop_agent as ctrl_stop, set_mode,
    confirm_trade, skip_trade,
    get_pending_trades, load_pending_trades
)
from price_fetcher import get_token_price_with_fallback as _fetch_price

PORT = 8080

agent_process = None
agent_lock    = threading.Lock()
AGENT_PID_FILE = "agent.pid"


def _kill_pid(pid):
    """Kill a process by PID — works even after dashboard restart."""
    try:
        import signal
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        try:
            os.kill(pid, 9)  # force kill if still alive
        except:
            pass
    except:
        pass


def _read_pid():
    try:
        if os.path.exists(AGENT_PID_FILE):
            pid = int(open(AGENT_PID_FILE).read().strip())
            return pid
    except:
        pass
    return None


def _clear_pid():
    try:
        if os.path.exists(AGENT_PID_FILE):
            os.remove(AGENT_PID_FILE)
    except:
        pass


def _write_pid(pid):
    try:
        with open(AGENT_PID_FILE, "w") as f:
            f.write(str(pid))
    except:
        pass


def start_agent():
    global agent_process
    with agent_lock:
        # Kill ANY leftover agent — in memory or from a previous dashboard session
        old_pid = _read_pid()
        if old_pid:
            _kill_pid(old_pid)
            _clear_pid()

        if agent_process and agent_process.poll() is None:
            agent_process.terminate()
            time.sleep(0.5)
            if agent_process.poll() is None:
                agent_process.kill()
            agent_process = None

        ctrl_start()
        agent_process = subprocess.Popen(
            [sys.executable, "main_agent.py"],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        _write_pid(agent_process.pid)   # remember PID across dashboard restarts
        return {"status": "started"}


def stop_agent():
    global agent_process
    with agent_lock:
        ctrl_stop()

        try:
            from main_agent import liquidate_all_holdings
            print("Liquidating holdings before stopping...")
            liquidate_all_holdings()
        except Exception as e:
            print(f"Error during liquidation: {e}")

        # Kill by PID file first (survives dashboard restarts)
        old_pid = _read_pid()
        if old_pid:
            _kill_pid(old_pid)
            _clear_pid()

        # Also kill in-memory reference
        if agent_process and agent_process.poll() is None:
            agent_process.terminate()
            deadline = time.time() + 2
            while time.time() < deadline:
                if agent_process.poll() is not None:
                    break
                time.sleep(0.1)
            if agent_process.poll() is None:
                agent_process.kill()
            agent_process = None
            return {"status": "stopped"}
        return {"status": "not_running"}

# Agent settings
SETTINGS_FILE  = "agent_settings.json"

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


def save_settings_file(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_portfolio():
    if os.path.exists("portfolio.json"):
        with open("portfolio.json", "r") as f:
            return json.load(f)
    return {
        "usdt_balance"  : 8.70,
        "start_balance" : 8.70,
        "holdings"      : {},
        "trade_history" : [],
        "total_trades"  : 0,
        "winning_trades": 0,
        "mode"          : "REAL"
    }


def load_logs():
    if os.path.exists("agent_log.txt"):
        with open("agent_log.txt", "r", encoding="utf-8") as f:
            lines = f.readlines()
            return [l.strip() for l in lines[-50:]]
    return []


def get_current_price_with_fallback(symbol, buy_price, contract=""):
    """
    Resolves live price via price_fetcher (3-source fallback).
    Falls back to buy_price if nothing works.
    """
    price, is_live = _fetch_price(symbol, contract, buy_price)
    return price, is_live


def get_dashboard_data():
    portfolio    = load_portfolio()
    settings     = load_settings()
    holdings     = portfolio.get("holdings", {})
    trades       = portfolio.get("trade_history", [])
    total_trades = portfolio.get("total_trades", 0)
    usdt_balance = portfolio.get("usdt_balance", 100)
    logs         = load_logs()
    ctrl         = get_control()

    holdings_data  = []
    total_holdings = 0

    for symbol, h in holdings.items():
        buy_price     = h.get("buy_price", 0)
        amount        = h.get("amount", 0)
        contract      = h.get("contract", "")
        current_price, is_live = get_current_price_with_fallback(
            symbol, buy_price, contract
        )

        # ── Price sanity guard ────────────────────────────────────────────────
        # If the fetched price is >10x or <10x different from buy_price, it
        # almost certainly comes from a different token with the same symbol
        # (e.g. Bitget-listed PIXEL ≠ Solana meme PIXEL).
        # Fall back to buy_price so the dashboard never shows fake +4000% P&L.
        if is_live and buy_price > 0:
            ratio = current_price / buy_price
            if ratio > 10 or ratio < 0.1:
                current_price = buy_price   # show neutral, not fake profit
                is_live       = False       # mark as stale so P&L shows '--'
        # ─────────────────────────────────────────────────────────────────────

        value       = amount * current_price
        cost        = amount * buy_price
        pnl         = value - cost
        pnl_pct     = ((current_price - buy_price) / buy_price * 100) \
                      if buy_price > 0 else 0
        sl          = settings.get("stop_loss_pct", 5)
        tp          = settings.get("take_profit_pct", 15)
        stop_loss   = buy_price * (1 - sl / 100)
        take_profit = buy_price * (1 + tp / 100)
        total_holdings += value

        holdings_data.append({
            "symbol"       : symbol,
            "amount"       : round(amount, 4),
            "buy_price"    : buy_price,
            "current_price": current_price,
            "is_live"      : is_live,
            "value"        : round(value, 2),
            "pnl"          : round(pnl, 2) if is_live else None,
            "pnl_pct"      : round(pnl_pct, 2) if is_live else None,
            "stop_loss"    : round(stop_loss, 8),
            "take_profit"  : round(take_profit, 8)
        })

    start_balance = portfolio.get("start_balance", 8.70)
    total_value = usdt_balance + total_holdings
    profit_loss = total_value - start_balance
    pnl_pct     = (profit_loss / start_balance) * 100 if start_balance > 0 else 0

    win_rate = 0
    if total_trades > 0:
        wins     = portfolio.get("winning_trades", 0)
        win_rate = round((wins / max(total_trades, 1)) * 100, 1)

    # Get pending trade from agent_control
    pending_trades = get_pending_trades()
    pending_trade  = None
    if pending_trades:
        first_id   = list(pending_trades.keys())[0]
        trade      = pending_trades[first_id]
        pending_trade = {
            "active"    : True,
            "trade_id"  : first_id,
            "symbol"    : trade.get("symbol"),
            "decision"  : trade.get("decision"),
            "confidence": trade.get("confidence"),
            "reason"    : trade.get("reason"),
            "amount"    : trade.get("amount"),
            "strategy"  : trade.get("strategy", "")
        }

    return {
        "usdt_balance" : round(usdt_balance, 2),
        "total_value"  : round(total_value, 2),
        "profit_loss"  : round(profit_loss, 2),
        "pnl_pct"      : round(pnl_pct, 2),
        "total_trades" : total_trades,
        "win_rate"     : win_rate,
        "holdings"     : holdings_data,
        "trades"       : trades[-20:],
        "logs"         : logs[-30:],
        "agent_running": ctrl.get("running", False),
        "agent_mode"   : ctrl.get("mode", "manual"),
        "settings"     : settings,
        "pending_trade": pending_trade,
        "timestamp"    : datetime.now().strftime("%H:%M:%S")
    }


def execute_manual_trade(symbol, amount):
    portfolio = load_portfolio()

    if portfolio["usdt_balance"] < float(amount):
        return {"status": "error", "message": "Insufficient balance"}

    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/spot/market/tickers",
            params={"symbol": f"{symbol}USDT"},
            timeout=10
        )
        data  = r.json()
        price = float(data["data"][0]["lastPr"])
    except:
        return {"status": "error",
                "message": f"Could not get price for {symbol}"}

    amount        = float(amount)
    tokens_bought = amount / price

    portfolio["usdt_balance"] -= amount

    if symbol in portfolio["holdings"]:
        existing   = portfolio["holdings"][symbol]
        total_amt  = existing["amount"] + tokens_bought
        total_cost = (existing["amount"] * existing["buy_price"]) + amount
        avg_price  = total_cost / total_amt
        portfolio["holdings"][symbol] = {
            "amount"   : total_amt,
            "buy_price": avg_price,
            "symbol"   : symbol
        }
    else:
        portfolio["holdings"][symbol] = {
            "amount"   : tokens_bought,
            "buy_price": price,
            "symbol"   : symbol
        }

    trade = {
        "type"       : "BUY",
        "symbol"     : symbol,
        "amount_usdt": amount,
        "price"      : price,
        "tokens"     : tokens_bought,
        "time"       : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "via"        : "Manual Dashboard Trade"
    }
    portfolio["trade_history"].append(trade)
    portfolio["total_trades"] = portfolio.get("total_trades", 0) + 1

    with open("portfolio.json", "w") as f:
        json.dump(portfolio, f, indent=2)

    return {
        "status" : "success",
        "message": f"Bought {tokens_bought:.4f} {symbol} at ${price}"
    }


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Solana AI Trading Agent</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --sol-purple : #9945FF;
            --sol-green  : #14F195;
            --sol-blue   : #00C2FF;
            --bg-dark    : #0a0a0f;
            --bg-card    : #12121a;
            --bg-card2   : #1a1a26;
            --border     : rgba(153,69,255,0.15);
            --border-bright: rgba(153,69,255,0.4);
            --text       : #e8e8f0;
            --muted      : #6b6b8a;
            --red        : #ff4757;
            --green      : #14F195;
        }

        * { margin:0; padding:0; box-sizing:border-box; }

        body {
            background : var(--bg-dark);
            color      : var(--text);
            font-family: 'Inter', sans-serif;
            min-height : 100vh;
            overflow-x : hidden;
        }

        /* Solana gradient mesh background */
        body::before {
            content : '';
            position: fixed;
            inset   : 0;
            background:
                radial-gradient(ellipse 80% 50% at 20% -20%, rgba(153,69,255,0.15) 0%, transparent 60%),
                radial-gradient(ellipse 60% 40% at 80% 100%, rgba(20,241,149,0.08) 0%, transparent 60%);
            pointer-events: none;
            z-index       : 0;
        }

        /* Grid overlay */
        body::after {
            content   : '';
            position  : fixed;
            inset     : 0;
            background: linear-gradient(rgba(153,69,255,0.03) 1px, transparent 1px),
                        linear-gradient(90deg, rgba(153,69,255,0.03) 1px, transparent 1px);
            background-size: 32px 32px;
            pointer-events : none;
            z-index        : 0;
        }

        .container {
            max-width: 1440px;
            margin   : 0 auto;
            padding  : 20px;
            position : relative;
            z-index  : 1;
        }

        /* ── HEADER ── */
        header {
            display        : flex;
            align-items    : center;
            justify-content: space-between;
            padding        : 16px 24px;
            background     : rgba(18,18,26,0.8);
            border         : 1px solid var(--border);
            border-radius  : 16px;
            margin-bottom  : 20px;
            backdrop-filter: blur(20px);
            flex-wrap      : wrap;
            gap            : 12px;
        }

        .logo {
            display    : flex;
            align-items: center;
            gap        : 12px;
        }

        .logo-icon {
            width        : 40px;
            height       : 40px;
            border-radius: 12px;
            background   : linear-gradient(135deg, var(--sol-purple), var(--sol-green));
            display      : flex;
            align-items  : center;
            justify-content: center;
            font-size    : 20px;
            flex-shrink  : 0;
        }

        .logo-text h1 {
            font-family   : 'Space Grotesk', sans-serif;
            font-size     : 18px;
            font-weight   : 700;
            background    : linear-gradient(90deg, var(--sol-purple), var(--sol-green));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .logo-text span {
            font-size: 11px;
            color    : var(--muted);
        }

        .header-right {
            display    : flex;
            align-items: center;
            gap        : 10px;
            flex-wrap  : wrap;
        }

        .status-badge {
            display      : flex;
            align-items  : center;
            gap          : 6px;
            padding      : 6px 14px;
            border-radius: 20px;
            font-size    : 12px;
            font-weight  : 500;
            border       : 1px solid;
        }

        .status-badge.running {
            background  : rgba(20,241,149,0.1);
            border-color: var(--sol-green);
            color       : var(--sol-green);
        }

        .status-badge.stopped {
            background  : rgba(255,71,87,0.1);
            border-color: var(--red);
            color       : var(--red);
        }

        .status-dot {
            width        : 7px;
            height       : 7px;
            border-radius: 50%;
            background   : currentColor;
        }

        .status-dot.pulse { animation: pulse 2s infinite; }

        @keyframes pulse {
            0%,100% { opacity:1; transform:scale(1); }
            50%     { opacity:0.5; transform:scale(0.7); }
        }

        .btn {
            padding      : 8px 18px;
            border-radius: 10px;
            font-size    : 13px;
            font-weight  : 600;
            cursor       : pointer;
            border       : none;
            transition   : all 0.2s;
            font-family  : 'Inter', sans-serif;
        }

        .btn:active { transform: scale(0.97); }

        .btn-start {
            background: linear-gradient(135deg, var(--sol-purple), #7c3aed);
            color     : white;
            box-shadow: 0 4px 15px rgba(153,69,255,0.3);
        }

        .btn-start:hover { box-shadow: 0 6px 20px rgba(153,69,255,0.5); }

        .btn-stop {
            background: rgba(255,71,87,0.15);
            color     : var(--red);
            border    : 1px solid rgba(255,71,87,0.3);
        }

        .btn-stop:hover { background: rgba(255,71,87,0.25); }

        .btn-disabled {
            opacity        : 0.35;
            cursor         : not-allowed;
            pointer-events : none;
            filter         : grayscale(40%);
        }

        .btn-trade {
            background: linear-gradient(135deg, var(--sol-green), #00c896);
            color     : #0a0a0f;
            font-weight: 700;
            box-shadow : 0 4px 15px rgba(20,241,149,0.25);
        }

        .btn-trade:hover { box-shadow: 0 6px 20px rgba(20,241,149,0.4); }

        .btn-sm {
            padding  : 6px 14px;
            font-size: 12px;
        }

        .update-time {
            font-size: 11px;
            color    : var(--muted);
        }

        /* ── STATS GRID ── */
        .stats-grid {
            display              : grid;
            grid-template-columns: repeat(6, 1fr);
            gap                  : 12px;
            margin-bottom        : 16px;
        }

        .stat-card {
            background   : var(--bg-card);
            border       : 1px solid var(--border);
            border-radius: 14px;
            padding      : 18px 16px;
            position     : relative;
            overflow     : hidden;
            transition   : border-color 0.3s, transform 0.2s;
        }

        .stat-card:hover {
            border-color: var(--border-bright);
            transform   : translateY(-2px);
        }

        .stat-card::before {
            content   : '';
            position  : absolute;
            top:0; left:0; right:0;
            height    : 2px;
            background: linear-gradient(90deg, var(--sol-purple), var(--sol-green));
        }

        .stat-label {
            font-size     : 10px;
            color         : var(--muted);
            letter-spacing: 1.5px;
            text-transform: uppercase;
            margin-bottom : 8px;
            font-weight   : 500;
        }

        .stat-value {
            font-family: 'Space Grotesk', sans-serif;
            font-size  : 24px;
            font-weight: 700;
            line-height: 1;
            transition : color 0.4s;
        }

        .stat-sub {
            font-size : 11px;
            color     : var(--muted);
            margin-top: 5px;
        }

        /* ── PENDING TRADE ALERT ── */
        .trade-alert {
            background   : rgba(153,69,255,0.1);
            border       : 1px solid var(--sol-purple);
            border-radius: 14px;
            padding      : 20px 24px;
            margin-bottom: 16px;
            display      : none;
            animation    : slideIn 0.3s ease;
        }

        .trade-alert.visible { display: block; }

        @keyframes slideIn {
            from { opacity:0; transform:translateY(-10px); }
            to   { opacity:1; transform:translateY(0); }
        }

        .trade-alert-header {
            display        : flex;
            align-items    : center;
            justify-content: space-between;
            margin-bottom  : 16px;
            flex-wrap      : wrap;
            gap            : 10px;
        }

        .trade-alert h3 {
            font-family: 'Space Grotesk', sans-serif;
            font-size  : 16px;
            font-weight: 700;
            color      : var(--sol-purple);
        }

        .trade-info-grid {
            display              : grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap                  : 12px;
            margin-bottom        : 16px;
        }

        .trade-info-item {
            background   : rgba(0,0,0,0.3);
            border-radius: 8px;
            padding      : 10px 14px;
        }

        .trade-info-label {
            font-size  : 10px;
            color      : var(--muted);
            margin-bottom: 4px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .trade-info-value {
            font-family: 'Space Grotesk', sans-serif;
            font-size  : 15px;
            font-weight: 600;
        }

        .trade-buttons {
            display: flex;
            gap    : 10px;
        }

        .btn-confirm {
            background: linear-gradient(135deg, var(--sol-green), #00c896);
            color     : #0a0a0f;
            font-weight: 700;
            flex      : 1;
            padding   : 12px;
            box-shadow: 0 4px 15px rgba(20,241,149,0.3);
        }

        .btn-reject {
            background: rgba(255,71,87,0.15);
            color     : var(--red);
            border    : 1px solid rgba(255,71,87,0.3);
            flex      : 1;
            padding   : 12px;
        }

        /* ── MAIN GRID ── */
        .main-grid {
            display              : grid;
            grid-template-columns: 1fr 1fr;
            gap                  : 16px;
            margin-bottom        : 16px;
        }

        .full-width { grid-column: 1 / -1; }

        .card {
            background   : var(--bg-card);
            border       : 1px solid var(--border);
            border-radius: 14px;
            overflow     : hidden;
        }

        .card-header {
            display        : flex;
            align-items    : center;
            justify-content: space-between;
            padding        : 14px 20px;
            border-bottom  : 1px solid var(--border);
            background     : var(--bg-card2);
        }

        .card-title {
            font-family   : 'Space Grotesk', sans-serif;
            font-size     : 13px;
            font-weight   : 600;
            letter-spacing: 0.5px;
            background    : linear-gradient(90deg, var(--sol-purple), var(--sol-blue));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .card-badge {
            font-size    : 11px;
            color        : var(--muted);
            background   : rgba(0,0,0,0.3);
            padding      : 3px 10px;
            border-radius: 20px;
            border       : 1px solid var(--border);
        }

        /* ── SETTINGS PANEL ── */
        .settings-grid {
            display              : grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap                  : 16px;
            padding              : 20px;
        }

        .setting-item label {
            display    : block;
            font-size  : 11px;
            color      : var(--muted);
            margin-bottom: 6px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .setting-item input {
            width        : 100%;
            background   : rgba(0,0,0,0.4);
            border       : 1px solid var(--border);
            border-radius: 8px;
            padding      : 8px 12px;
            color        : var(--text);
            font-size    : 14px;
            font-family  : 'Space Grotesk', sans-serif;
            font-weight  : 600;
            transition   : border-color 0.2s;
        }

        .setting-item input:focus {
            outline     : none;
            border-color: var(--sol-purple);
        }

        .settings-save {
            padding: 0 20px 20px;
        }

        /* ── MANUAL TRADE ── */
        .manual-trade {
            padding: 20px;
        }

        .trade-inputs {
            display: flex;
            gap    : 10px;
            flex-wrap: wrap;
        }

        .trade-inputs input {
            flex         : 1;
            min-width    : 120px;
            background   : rgba(0,0,0,0.4);
            border       : 1px solid var(--border);
            border-radius: 8px;
            padding      : 10px 14px;
            color        : var(--text);
            font-size    : 14px;
            font-family  : 'Space Grotesk', sans-serif;
            font-weight  : 500;
            transition   : border-color 0.2s;
        }

        .trade-inputs input:focus {
            outline     : none;
            border-color: var(--sol-green);
        }

        .trade-inputs input::placeholder { color: var(--muted); }

        .trade-result {
            margin-top   : 10px;
            padding      : 10px 14px;
            border-radius: 8px;
            font-size    : 13px;
            display      : none;
        }

        .trade-result.success {
            background: rgba(20,241,149,0.1);
            border    : 1px solid rgba(20,241,149,0.3);
            color     : var(--sol-green);
            display   : block;
        }

        .trade-result.error {
            background: rgba(255,71,87,0.1);
            border    : 1px solid rgba(255,71,87,0.3);
            color     : var(--red);
            display   : block;
        }

        /* ── TABLE ── */
        table { width:100%; border-collapse:collapse; }

        th {
            padding       : 10px 16px;
            text-align    : left;
            font-size     : 10px;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            color         : var(--muted);
            border-bottom : 1px solid var(--border);
            background    : var(--bg-card2);
            font-weight   : 500;
        }

        td {
            padding      : 12px 16px;
            font-size    : 13px;
            border-bottom: 1px solid rgba(153,69,255,0.06);
            transition   : background 0.2s;
        }

        tr:hover td { background: rgba(153,69,255,0.04); }
        tr:last-child td { border-bottom: none; }

        .empty {
            color     : var(--muted);
            text-align: center;
            padding   : 30px !important;
            font-style: italic;
        }

        .token-badge {
            background   : rgba(153,69,255,0.1);
            border       : 1px solid rgba(153,69,255,0.3);
            color        : var(--sol-purple);
            padding      : 3px 10px;
            border-radius: 6px;
            font-size    : 12px;
            font-weight  : 600;
        }

        .pnl-pos { color: var(--sol-green); font-weight:600; }
        .pnl-neg { color: var(--red);       font-weight:600; }

        /* ── LOG ── */
        .log-wrap {
            padding   : 14px 16px;
            max-height: 260px;
            overflow-y: auto;
        }

        .log-wrap::-webkit-scrollbar { width:3px; }
        .log-wrap::-webkit-scrollbar-track { background:transparent; }
        .log-wrap::-webkit-scrollbar-thumb {
            background   : var(--border);
            border-radius: 2px;
        }

        .log-line {
            font-size    : 11px;
            font-family  : 'Space Grotesk', monospace;
            padding      : 3px 0;
            border-bottom: 1px solid rgba(153,69,255,0.06);
            line-height  : 1.7;
        }

        /* ── FLASH ANIMATION ── */
        @keyframes flash {
            0%   { background: rgba(153,69,255,0.15); }
            100% { background: transparent; }
        }
        .flash { animation: flash 0.6s ease; }

        /* ── FOOTER ── */
        footer {
            text-align : center;
            padding    : 16px;
            color      : var(--muted);
            font-size  : 11px;
            border-top : 1px solid var(--border);
            margin-top : 16px;
        }

        footer span {
            background: linear-gradient(90deg, var(--sol-purple), var(--sol-green));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            font-weight: 600;
        }

        /* ── RESPONSIVE ── */
        @media (max-width: 1100px) {
            .stats-grid { grid-template-columns: repeat(3, 1fr); }
        }

        @media (max-width: 768px) {
            .stats-grid { grid-template-columns: repeat(2, 1fr); }
            .main-grid  { grid-template-columns: 1fr; }
            header      { padding: 12px 16px; }
            .container  { padding: 12px; }
            .stat-value { font-size: 20px; }
        }

        @media (max-width: 480px) {
            .stats-grid     { grid-template-columns: 1fr 1fr; gap:8px; }
            .stat-card      { padding: 14px 12px; }
            .btn            { padding: 7px 12px; font-size:12px; }
            .header-right   { gap: 6px; }
            .trade-inputs   { flex-direction: column; }
        }
    </style>
</head>
<body>
<div class="container">

    <!-- Header -->
    <header>
        <div class="logo">
            <div class="logo-icon">◎</div>
            <div class="logo-text">
                <h1>Solana AI Trading Agent</h1>
                <span>Bitget Wallet Skill API · Groq Llama 3.3 · Solana</span>
            </div>
        </div>
        <div class="header-right">
            <div class="status-badge stopped" id="agentStatus">
                <div class="status-dot" id="statusDot"></div>
                <span id="statusText">Stopped</span>
            </div>
            <span class="update-time" id="updateTime">--</span>
            <button class="btn btn-start" onclick="startAgent()">▶ Start Agent</button>
            <button class="btn btn-stop"  onclick="stopAgent()">■ Stop</button>
        </div>
    </header>

    <!-- Stats -->
    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-label">USDT Balance</div>
            <div class="stat-value" id="usdtBalance"
                 style="color:var(--sol-blue)">--</div>
            <div class="stat-sub">Available</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Total Value</div>
            <div class="stat-value" id="totalValue">--</div>
            <div class="stat-sub">Portfolio</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Total P&L</div>
            <div class="stat-value" id="totalPnl">--</div>
            <div class="stat-sub" id="pnlPct">--</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Win Rate</div>
            <div class="stat-value" id="winRate"
                 style="color:var(--sol-green)">--</div>
            <div class="stat-sub">Of all trades</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Total Trades</div>
            <div class="stat-value" id="totalTrades">--</div>
            <div class="stat-sub">Executed</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Positions</div>
            <div class="stat-value" id="openPositions"
                 style="color:var(--sol-purple)">--</div>
            <div class="stat-sub">Open now</div>
        </div>
    </div>

    <!-- Pending Trade Alert -->
    <div class="trade-alert" id="tradeAlert">
        <div class="trade-alert-header">
            <h3>⚡ Trade Confirmation Required</h3>
            <div id="alertTimer" style="font-size:12px;color:var(--muted)"></div>
        </div>
        <div class="trade-info-grid" id="tradeInfoGrid"></div>
        <div class="trade-buttons">
            <button class="btn btn-confirm" onclick="confirmTrade()">
                ✅ CONFIRM TRADE
            </button>
            <button class="btn btn-reject btn" onclick="rejectTrade()">
                ❌ SKIP
            </button>
        </div>
    </div>

    <!-- Controls Row -->
    <div class="main-grid">

        <!-- Settings -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">⚙️ Agent Settings</span>
                <span class="card-badge">Live config</span>
            </div>
            <div class="settings-grid">
                <div class="setting-item">
                    <label>Stop Loss %</label>
                    <input type="number" id="stopLoss"
                           value="5" min="1" max="20" step="0.5">
                </div>
                <div class="setting-item">
                    <label>Take Profit %</label>
                    <input type="number" id="takeProfit"
                           value="15" min="5" max="100" step="1">
                </div>
                <div class="setting-item">
                    <label>Scan Interval (mins)</label>
                    <input type="number" id="scanInterval"
                           value="15" min="5" max="60" step="5">
                </div>
                <div class="setting-item">
                    <label>Max Trade ($)</label>
                    <input type="number" id="maxTrade"
                           value="10" min="1" max="50" step="1">
                </div>
            </div>
            <div class="settings-save">
                <button class="btn btn-start btn-sm"
                        onclick="saveSettings()" style="width:100%">
                    💾 Save Settings
                </button>
            </div>
        </div>

        <!-- Log -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">📡 Agent Log</span>
                <span class="card-badge">Live</span>
            </div>
            <div class="log-wrap" id="logWrap"></div>
        </div>

        <!-- Holdings -->
        <div class="card full-width">
            <div class="card-header">
                <span class="card-title">📊 Live Holdings</span>
                <span class="card-badge" id="holdingsCount">0 positions</span>
            </div>
            <div style="overflow-x:auto">
                <table>
                    <thead>
                        <tr>
                            <th>Token</th>
                            <th>Amount</th>
                            <th>Buy Price</th>
                            <th>Current</th>
                            <th>Value</th>
                            <th>P&L</th>
                            <th>Stop Loss</th>
                            <th>Take Profit</th>
                        </tr>
                    </thead>
                    <tbody id="holdingsBody">
                        <tr><td colspan="8" class="empty">
                            No open positions
                        </td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Manual Trade -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">🎯 Manual Trade</span>
                <span class="card-badge">Paper trading</span>
            </div>
            <div class="manual-trade">
                <p style="font-size:12px;color:var(--muted);margin-bottom:14px">
                    Buy any Solana token manually — enter symbol and amount in USDT
                </p>
                <div class="trade-inputs">
                    <input type="text" id="tradeSymbol"
                           placeholder="Token (e.g. WIF, BONK, SOL)"
                           style="text-transform:uppercase">
                    <input type="number" id="tradeAmount"
                           placeholder="Amount in USDT" min="1" max="50">
                    <button class="btn btn-trade"
                            onclick="executeTrade()">BUY</button>
                </div>
                <div class="trade-result" id="tradeResult"></div>
            </div>
        </div>

        <!-- Trade History -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">📋 Trade History</span>
                <span class="card-badge" id="tradesCount">0 trades</span>
            </div>
            <div style="overflow-x:auto;max-height:280px;overflow-y:auto">
                <table>
                    <thead>
                        <tr>
                            <th>Type</th>
                            <th>Token</th>
                            <th>Amount</th>
                            <th>Price</th>
                            <th>P&L</th>
                            <th>Time</th>
                        </tr>
                    </thead>
                    <tbody id="tradesBody">
                        <tr><td colspan="5" class="empty">
                            No trades yet
                        </td></tr>
                    </tbody>
                </table>
            </div>
        </div>

    </div>
</div>

<footer>
    <span>Solana AI Trading Agent</span> —
    Bitget Wallet Hackathon 2026 —
    Built with Bitget Wallet Skill API + Groq Llama 3.3
</footer>

<script>
let currentTradeId   = null;
let settingsLoaded   = false;  // sync settings from server only on first load
let dismissedTradeIds = new Set(); // prevent re-showing after confirm/skip

function flash(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
}

async function startAgent() {
    const res  = await fetch('/agent/start', {method:'POST'});
    const data = await res.json();
    console.log('Start:', data.status);
    updateDashboard();
}

async function stopAgent() {
    const res  = await fetch('/agent/stop', {method:'POST'});
    const data = await res.json();
    console.log('Stop:', data.status);
    updateDashboard();
}

async function saveSettings() {
    const settings = {
        stop_loss_pct    : parseFloat(document.getElementById('stopLoss').value),
        take_profit_pct  : parseFloat(document.getElementById('takeProfit').value),
        scan_interval_mins: parseInt(document.getElementById('scanInterval').value),
        max_trade_amount : parseFloat(document.getElementById('maxTrade').value)
    };
    const res  = await fetch('/settings', {
        method : 'POST',
        headers: {'Content-Type':'application/json'},
        body   : JSON.stringify(settings)
    });
    const data = await res.json();
    if (data.status === 'saved') {
        const btn = event.target;
        btn.textContent = '✅ Saved!';
        setTimeout(() => btn.textContent = '💾 Save Settings', 2000);
    }
}

async function executeTrade() {
    const symbol = document.getElementById('tradeSymbol').value.toUpperCase().trim();
    const amount = document.getElementById('tradeAmount').value;
    const result = document.getElementById('tradeResult');

    if (!symbol || !amount) {
        result.className = 'trade-result error';
        result.textContent = 'Please enter token symbol and amount';
        return;
    }

    result.className    = 'trade-result';
    result.textContent  = 'Executing...';
    result.style.display = 'block';

    const res  = await fetch('/trade/manual', {
        method : 'POST',
        headers: {'Content-Type':'application/json'},
        body   : JSON.stringify({symbol, amount})
    });
    const data = await res.json();

    if (data.status === 'success') {
        result.className = 'trade-result success';
        result.textContent = '✅ ' + data.message;
        document.getElementById('tradeSymbol').value = '';
        document.getElementById('tradeAmount').value = '';
    } else {
        result.className = 'trade-result error';
        result.textContent = '❌ ' + data.message;
    }

    setTimeout(() => {
        result.style.display = 'none';
    }, 4000);
}

async function confirmTrade() {
    if (!currentTradeId) return;
    const tid = currentTradeId;
    dismissedTradeIds.add(tid);   // dismiss immediately — don’t wait for server
    document.getElementById('tradeAlert').classList.remove('visible');
    currentTradeId = null;
    await fetch('/trade/confirm', {
        method : 'POST',
        headers: {'Content-Type':'application/json'},
        body   : JSON.stringify({trade_id: tid, action: 'confirm'})
    });
}

async function rejectTrade() {
    if (!currentTradeId) return;
    const tid = currentTradeId;
    dismissedTradeIds.add(tid);   // dismiss immediately
    document.getElementById('tradeAlert').classList.remove('visible');
    currentTradeId = null;
    await fetch('/trade/confirm', {
        method : 'POST',
        headers: {'Content-Type':'application/json'},
        body   : JSON.stringify({trade_id: tid, action: 'skip'})
    });
}

async function updateDashboard() {
    try {
        const res  = await fetch('/data');
        const data = await res.json();

        // Stats
        document.getElementById('usdtBalance').textContent =
            '$' + data.usdt_balance.toFixed(2);
        document.getElementById('totalValue').textContent =
            '$' + data.total_value.toFixed(2);
        document.getElementById('totalTrades').textContent =
            data.total_trades;
        document.getElementById('winRate').textContent =
            data.win_rate + '%';
        document.getElementById('openPositions').textContent =
            data.holdings.length;

        const pnl    = data.profit_loss;
        const pnlEl  = document.getElementById('totalPnl');
        const pnlPct = document.getElementById('pnlPct');
        pnlEl.textContent  = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
        pnlEl.style.color  = pnl >= 0 ? 'var(--sol-green)' : 'var(--red)';
        pnlPct.textContent = (pnl >= 0 ? '+' : '') + data.pnl_pct.toFixed(2) + '%';
        flash('totalPnl');

        // Agent status + button state
        const badge   = document.getElementById('agentStatus');
        const dot     = document.getElementById('statusDot');
        const txt     = document.getElementById('statusText');
        const btnStart = document.querySelector('.btn-start:not(.btn-sm)');
        const btnStop  = document.querySelector('.btn-stop');
        if (data.agent_running) {
            badge.className  = 'status-badge running';
            dot.className    = 'status-dot pulse';
            txt.textContent  = 'Running';
            // Dim Start, enable Stop
            btnStart.classList.add('btn-disabled');
            btnStop.classList.remove('btn-disabled');
        } else {
            badge.className  = 'status-badge stopped';
            dot.className    = 'status-dot';
            txt.textContent  = 'Stopped';
            // Enable Start, dim Stop
            btnStart.classList.remove('btn-disabled');
            btnStop.classList.add('btn-disabled');
        }

        // Settings sync — only on first load so the user can freely edit values
        if (data.settings && !settingsLoaded) {
            document.getElementById('stopLoss').value =
                data.settings.stop_loss_pct;
            document.getElementById('takeProfit').value =
                data.settings.take_profit_pct;
            document.getElementById('scanInterval').value =
                data.settings.scan_interval_mins;
            document.getElementById('maxTrade').value =
                data.settings.max_trade_amount;
            settingsLoaded = true;  // never overwrite again until page refresh
        }

        // Pending trade alert — skip if we already dismissed this trade
        if (data.pending_trade && data.pending_trade.active
                && !dismissedTradeIds.has(data.pending_trade.trade_id)) {
            const pt    = data.pending_trade;
            currentTradeId = pt.trade_id;
            document.getElementById('tradeAlert').classList.add('visible');
            document.getElementById('tradeInfoGrid').innerHTML = `
                <div class="trade-info-item">
                    <div class="trade-info-label">Token</div>
                    <div class="trade-info-value"
                         style="color:var(--sol-purple)">${pt.symbol}</div>
                </div>
                <div class="trade-info-item">
                    <div class="trade-info-label">Decision</div>
                    <div class="trade-info-value"
                         style="color:var(--sol-green)">${pt.decision}</div>
                </div>
                <div class="trade-info-item">
                    <div class="trade-info-label">Confidence</div>
                    <div class="trade-info-value">${pt.confidence}</div>
                </div>
                <div class="trade-info-item">
                    <div class="trade-info-label">Amount</div>
                    <div class="trade-info-value">$${pt.amount}</div>
                </div>
                <div class="trade-info-item" style="grid-column:1/-1">
                    <div class="trade-info-label">AI Reason</div>
                    <div class="trade-info-value"
                         style="font-size:13px;font-weight:400">
                        ${pt.reason}
                    </div>
                </div>
            `;
        }

        // Holdings
        const hBody = document.getElementById('holdingsBody');
        document.getElementById('holdingsCount').textContent =
            data.holdings.length + ' positions';
        if (data.holdings.length === 0) {
            hBody.innerHTML =
                '<tr><td colspan="8" class="empty">No open positions</td></tr>';
        } else {
            hBody.innerHTML = data.holdings.map(h => {
                const cls  = h.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
                const sign = h.pnl >= 0 ? '+' : '';
                return `<tr>
                    <td><span class="token-badge">${h.symbol}</span></td>
                    <td>${h.amount}</td>
                    <td>$${h.buy_price}</td>
                    <td>$${h.current_price}</td>
                    <td>$${h.value}</td>
                    <td class="${h.is_live ? cls : ''}" style="${!h.is_live ? 'color:#4a6070' : ''}">
                        ${h.is_live
                            ? `${sign}$${h.pnl} (${sign}${h.pnl_pct}%)`
                            : 'Price unavailable'
                        }
                    </td>
                    <td style="color:var(--red);font-size:12px">
                        $${h.stop_loss}
                    </td>
                    <td style="color:var(--sol-green);font-size:12px">
                        $${h.take_profit}
                    </td>
                </tr>`;
            }).join('');
        }

        // Logs
        const logWrap = document.getElementById('logWrap');
        logWrap.innerHTML = [...data.logs].reverse().map(log => {
            let color = '#6b6b8a';
            if (log.includes('ERROR'))          color = '#ff4757';
            else if (log.includes('BUY'))       color = '#14F195';
            else if (log.includes('SELL'))      color = '#ffaa00';
            else if (log.includes('PROFIT'))    color = '#14F195';
            else if (log.includes('LOSS'))      color = '#ff4757';
            else if (log.includes('STOP'))      color = '#ff4757';
            else if (log.includes('started'))   color = '#9945FF';
            return `<div class="log-line" style="color:${color}">${log}</div>`;
        }).join('');

        // Trades
        const tBody = document.getElementById('tradesBody');
        document.getElementById('tradesCount').textContent =
            data.trades.length + ' trades';
        if (data.trades.length === 0) {
            tBody.innerHTML =
                '<tr><td colspan="5" class="empty">No trades yet</td></tr>';
        } else {
            tBody.innerHTML = [...data.trades].reverse().map(t => {
                const isBuy  = t.type === 'BUY';
                const c      = isBuy ? 'var(--sol-green)' : 'var(--red)';
                const amount = isBuy
                    ? parseFloat(t.amount_usdt || 0).toFixed(2)
                    : parseFloat(t.sell_value  || t.amount_usdt || 0).toFixed(2);
                const price  = isBuy
                    ? parseFloat(t.price      || 0).toFixed(8)
                    : parseFloat(t.sell_price || t.price || 0).toFixed(8);
                const pnlColor = t.profit_loss >= 0 ? 'var(--sol-green)' : 'var(--red)';
                const pnl = !isBuy && t.profit_loss != null
                    ? '<span style="color:' + pnlColor + '">'
                      + (t.profit_loss >= 0 ? '+' : '')
                      + '$' + parseFloat(t.profit_loss).toFixed(2) + ' '
                      + '(' + (t.pct_change != null ? parseFloat(t.pct_change).toFixed(1) : '?') + '%)'
                      + '</span>'
                    : '';
                return '<tr>'
                    + '<td style="color:' + c + ';font-weight:700">' + t.type + '</td>'
                    + '<td><span class="token-badge">' + t.symbol + '</span></td>'
                    + '<td>$' + amount + '</td>'
                    + '<td>$' + price  + '</td>'
                    + '<td>' + pnl + '</td>'
                    + '<td style="font-size:11px;color:var(--muted)">' + t.time + '</td>'
                    + '</tr>';
            }).join('');
        }

        document.getElementById('updateTime').textContent =
            'Updated ' + data.timestamp;

    } catch(e) {
        console.error('Update error:', e);
    }
}

updateDashboard();
setInterval(updateDashboard, 5000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        if self.path == "/data":
            self.send_json(get_dashboard_data())
        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        if self.path == "/agent/start":
           self.send_json(start_agent())

        elif self.path == "/agent/stop":
            self.send_json(stop_agent())


        elif self.path == "/agent/mode":
            body = self.read_body()
            set_mode(body.get("mode", "manual"))
            self.send_json({"status": "ok"})

        elif self.path == "/settings":
            body = self.read_body()
            save_settings_file(body)
            self.send_json({"status": "saved"})

        elif self.path == "/trade/manual":
            body   = self.read_body()
            result = execute_manual_trade(
                body.get("symbol", ""),
                body.get("amount", 5)
            )
            self.send_json(result)

        elif self.path == "/trade/confirm":
            body   = self.read_body()
            tid    = body.get("trade_id", "")
            action = body.get("action", "skip")
            if action == "confirm":
                confirm_trade(tid)
            else:
                skip_trade(tid)
            self.send_json({"status": "ok"})

        else:
            self.send_json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print("=" * 60)
    print("  Solana AI Trading Agent - Command Center")
    print("=" * 60)
    print(f"\nDashboard: http://localhost:{PORT}")
    print("Full Solana-themed command center!")
    print("Press Ctrl+C to stop\n")
    # Kill any agent left over from a previous dashboard session
    old = _read_pid()
    if old:
        print(f"Killing leftover agent process (PID {old})...")
        _kill_pid(old)
        _clear_pid()
    # Reset stale running state
    ctrl_stop()
    server = HTTPServer(("", PORT), Handler)
    server.serve_forever()



'''

---

## ▶️ Run It
```
python dashboard_server.py

'''