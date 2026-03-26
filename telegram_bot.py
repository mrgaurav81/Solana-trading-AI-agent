import requests
import time
import threading
from datetime import datetime
from agent_control import (
    start_agent, stop_agent, set_mode,
    get_control, confirm_trade, skip_trade,
    load_pending_trades
)

import os
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = "5073017116"

last_update_id    = 0
bot_running       = True
_bot_started      = False           # prevent duplicate polling threads
_seen_update_ids  = set()           # deduplicate updates across threads

# Internal rate-limit timestamps (prevents duplicate messages)
import threading as _threading
_notify_lock         = _threading.Lock()  # protects all rate-limiters below
_last_portfolio_msg  = datetime.min   # max once per hour
_last_scan_msg       = datetime.min   # max once per 10 minutes


def send(text):
    """
    Sends plain text message.
    """
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id"   : CHAT_ID,
                "text"      : text,
                "parse_mode": "HTML"
            },
            timeout=10
        )
    except Exception as e:
        print(f"Telegram send error: {e}")


def send_buttons(text, buttons):
    """
    Sends message with inline keyboard buttons.
    buttons = [[{"text": "...", "callback_data": "..."}]]
    """
    try:
        import json
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id"     : CHAT_ID,
                "text"        : text,
                "parse_mode"  : "HTML",
                "reply_markup": json.dumps({"inline_keyboard": buttons})
            },
            timeout=10
        )
    except Exception as e:
        print(f"Telegram button error: {e}")


def answer_callback(callback_id, text=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            data={"callback_query_id": callback_id, "text": text},
            timeout=5
        )
    except:
        pass


def edit_message(message_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            data={
                "chat_id"   : CHAT_ID,
                "message_id": message_id,
                "text"      : text,
                "parse_mode": "HTML"
            },
            timeout=5
        )
    except:
        pass


def handle_command(text, message_id=None):
    """
    Handles Telegram commands from user.
    """
    text = text.strip().lower()

    if text == "/start" or text == "start":
        start_agent()
        send("""✅ <b>Agent Started!</b>

The Solana AI Trading Agent is now running.
Scanning every 15 minutes for opportunities.

Commands:
/stop - Stop the agent
/status - View portfolio
/holdings - View open positions
/auto - Switch to auto mode
/manual - Switch to manual mode
/help - Show all commands""")

    elif text == "/stop" or text == "stop":
        stop_agent()
        send("🛑 <b>Agent Stopped!</b>\nThe agent has been stopped.")

    elif text == "/auto":
        set_mode("auto")
        send("🤖 <b>Auto Mode ON</b>\nAgent will trade automatically without asking.")

    elif text == "/manual":
        set_mode("manual")
        send("👤 <b>Manual Mode ON</b>\nAgent will ask you before every trade.")

    elif text == "/status":
        try:
            import json
            import os
            if os.path.exists("portfolio.json"):
                with open("portfolio.json", "r") as f:
                    p = json.load(f)
                balance     = p.get("usdt_balance", 0)
                trades      = p.get("total_trades", 0)
                holdings    = p.get("holdings", {})
                total_value = balance
                for s, h in holdings.items():
                    total_value += h.get("amount", 0) * h.get("buy_price", 0)
                pnl = total_value - 100
                ctrl = get_control()
                mode = ctrl.get("mode", "manual")
                status = "🟢 Running" if ctrl.get("running") else "🔴 Stopped"

                send(f"""📊 <b>Portfolio Status</b>

Status       : {status}
Mode         : {mode.upper()}
USDT Balance : ${balance:.2f}
Total Value  : ${total_value:.2f}
P&L          : {"+" if pnl >= 0 else ""}${pnl:.2f}
Total Trades : {trades}
Open Positions: {len(holdings)}

Time: {datetime.now().strftime('%H:%M:%S')}""")
            else:
                send("No portfolio data yet.")
        except Exception as e:
            send(f"Error reading portfolio: {e}")

    elif text == "/holdings":
        try:
            import json
            import os
            if os.path.exists("portfolio.json"):
                with open("portfolio.json", "r") as f:
                    p = json.load(f)
                holdings = p.get("holdings", {})
                if not holdings:
                    send("No open positions.")
                    return
                msg = "💼 <b>Open Positions</b>\n\n"
                for symbol, h in holdings.items():
                    amount    = h.get("amount", 0)
                    buy_price = h.get("buy_price", 0)
                    value     = amount * buy_price
                    sl        = buy_price * 0.95
                    tp        = buy_price * 1.15
                    msg += f"""<b>{symbol}</b>
Amount    : {amount:.4f}
Buy Price : ${buy_price:.8f}
Value     : ${value:.2f}
Stop Loss : ${sl:.8f}
Take Profit: ${tp:.8f}
---
"""
                send(msg)
            else:
                send("No portfolio data yet.")
        except Exception as e:
            send(f"Error: {e}")

    elif text == "/help":
        send("""🤖 <b>Solana AI Trading Agent Commands</b>

<b>Agent Control:</b>
/start   — Start the agent
/stop    — Stop the agent
/auto    — Auto trading mode
/manual  — Manual confirmation mode

<b>Portfolio:</b>
/status   — Portfolio summary
/holdings — Open positions

<b>Info:</b>
/help — Show this message""")

    else:
        send(f"Unknown command: {text}\nSend /help for commands.")


def handle_callback(callback_id, data, message_id):
    """
    Handles button clicks (trade confirmations).
    """
    answer_callback(callback_id)

    if data.startswith("confirm_"):
        trade_id = data.replace("confirm_", "")
        confirm_trade(trade_id)
        edit_message(message_id, "✅ <b>Trade CONFIRMED!</b>\nExecuting now...")

    elif data.startswith("skip_"):
        trade_id = data.replace("skip_", "")
        skip_trade(trade_id)
        edit_message(message_id, "❌ <b>Trade SKIPPED</b>\nCancelled.")


def poll_updates():
    """
    Polls Telegram for new messages and button clicks.
    Runs in background thread.
    """
    global last_update_id, bot_running

    print("Telegram bot polling started...")

    while bot_running:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 10},
                timeout=15
            )
            updates = r.json()

            if updates.get("ok") and updates.get("result"):
                for update in updates["result"]:
                    uid = update["update_id"]
                    last_update_id = uid

                    # Skip if already handled by another thread
                    if uid in _seen_update_ids:
                        continue
                    _seen_update_ids.add(uid)
                    # Keep set bounded
                    if len(_seen_update_ids) > 500:
                        _seen_update_ids.clear()

                    # Handle regular messages
                    if "message" in update:
                        msg  = update["message"]
                        text = msg.get("text", "")
                        mid  = msg.get("message_id")
                        if text:
                            handle_command(text, mid)

                    # Handle button clicks
                    elif "callback_query" in update:
                        cb  = update["callback_query"]
                        cid = cb["id"]
                        dat = cb.get("data", "")
                        mid = cb["message"]["message_id"]
                        handle_callback(cid, dat, mid)

        except Exception as e:
            print(f"Poll error: {e}")
            time.sleep(5)

        time.sleep(1)


def start_bot():
    """
    Starts the Telegram bot in background thread.
    Only creates one thread regardless of how many times it is called.
    """
    global _bot_started
    if _bot_started:
        return None          # already running — do nothing
    _bot_started = True
    thread = threading.Thread(target=poll_updates, daemon=True)
    thread.start()
    return thread


def stop_bot():
    global bot_running
    bot_running = False


# ── Notification functions ────────────────────────────────────────

def notify_agent_started():
    send("""🤖 <b>Solana AI Trading Agent Started!</b>

🕐 Time: """ + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + """
🔄 Scanning every 15 minutes
💰 Starting balance: $100.00 USDT
🌐 Chain: Solana
📊 Data: Bitget Wallet Skill API
🧠 AI: Groq Llama 3.3

Commands: /stop /status /holdings /help""")


def notify_scan_complete(bullish_count, total_count):
    global _last_scan_msg
    with _notify_lock:
        now = datetime.now()
        if (now - _last_scan_msg).total_seconds() < 600:  # once per 10 min
            return
        _last_scan_msg = now
    send(f"""📊 <b>Market Scan Complete</b>

🕐 {now.strftime('%H:%M:%S')}
🔍 Tokens scanned : {total_count}
🟢 Bullish tokens : {bullish_count}
📡 Source: Bitget Wallet Skill Hotpicks""")


def notify_trade_confirmation(trade_id, symbol, decision,
                               confidence, reason, amount, strategy):
    """
    Sends trade confirmation with buttons.
    """
    import json
    text = f"""🤖 <b>Trade Confirmation Required</b>

Token      : <b>{symbol}</b>
Strategy   : <b>{strategy}</b>
Decision   : <b>{decision}</b>
Confidence : <b>{confidence}</b>
Amount     : <b>${amount}</b>
Reason     : {reason}

Tap to confirm or skip:"""

    buttons = [[
        {"text": "✅ CONFIRM", "callback_data": f"confirm_{trade_id}"},
        {"text": "❌ SKIP",    "callback_data": f"skip_{trade_id}"}
    ]]
    send_buttons(text, buttons)


def notify_trade_executed(trade_type, symbol, amount_usdt,
                           price, tokens, balance):
    emoji = "💚" if trade_type == "BUY" else "❤️"
    send(f"""{emoji} <b>Paper Trade Executed!</b>

Type    : {trade_type}
Token   : {symbol}
Amount  : ${amount_usdt}
Price   : ${price}
Tokens  : {tokens:.6f}
Balance : ${balance:.2f} USDT
Via     : Bitget Wallet Skill API
Time    : {datetime.now().strftime('%H:%M:%S')}""")


def notify_portfolio_status(balance, total_value,
                             profit_loss, total_trades):
    global _last_portfolio_msg
    with _notify_lock:
        now = datetime.now()
        if (now - _last_portfolio_msg).total_seconds() < 3600:  # once per hour
            return
        _last_portfolio_msg = now
    pnl_text = f"+${profit_loss:.2f} 📈" if profit_loss >= 0 \
               else f"-${abs(profit_loss):.2f} 📉"
    send(f"""📋 <b>Portfolio Update</b>

💵 USDT Balance : ${balance:.2f}
💼 Total Value  : ${total_value:.2f}
📊 P&L          : {pnl_text}
🔢 Total Trades : {total_trades}
🕐 Time         : {now.strftime('%H:%M:%S')}""")

def notify_no_trades(reason):
    send(f"""😴 <b>No Trades This Cycle</b>

Reason : {reason}
Time   : {datetime.now().strftime('%H:%M:%S')}
Next scan in 15 minutes...""")


def notify_stop_loss(symbol, reason, pct_change,
                     buy_price, sell_price, balance):
    emoji = "🛑" if reason == "STOP_LOSS" else "💰"
    title = "Stop-Loss Triggered!" if reason == "STOP_LOSS" \
            else "Take-Profit Triggered!"
    send(f"""{emoji} <b>{title}</b>

Token      : {symbol}
Reason     : {reason}
Change     : {pct_change}%
Buy Price  : ${buy_price}
Sell Price : ${sell_price}
Balance    : ${balance:.2f} USDT
Time       : {datetime.now().strftime('%H:%M:%S')}

Position automatically closed!""")


def notify_error(error_message):
    send(f"""❌ <b>Agent Error</b>

Error : {error_message}
Time  : {datetime.now().strftime('%H:%M:%S')}
Agent will retry in 15 minutes...""")


def notify_ai_decision(symbol, decision, confidence, reason, amount):
    emoji = "🟢" if decision == "BUY" else "🔴" if decision == "SELL" else "🟡"
    send(f"""{emoji} <b>AI Trading Decision</b>

Token      : {symbol}
Decision   : {decision}
Confidence : {confidence}
Reason     : {reason}
Amount     : ${amount}
AI Model   : Groq Llama 3.3""")


def notify_recovery_mode(active: bool, total_value: float):
    """Sent when portfolio enters or exits recovery mode."""
    if active:
        send(f"""⚠️ <b>Recovery Mode Activated</b>

Portfolio value dropped to <b>${total_value:.2f}</b> (below $70).

🛑 Buying paused — only selling allowed.
📈 Normal trading resumes when value recovers above $80.

Use /status to monitor progress.""")
    else:
        send(f"""✅ <b>Recovery Mode Deactivated</b>

Portfolio recovered to <b>${total_value:.2f}</b> — above $80.

▶️ Normal trading resumed!""")