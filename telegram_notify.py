import requests
import time
from datetime import datetime

import os
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = "5073017116"

# This stores pending trade confirmations
# Key = message_id, Value = trade details
pending_trades = {}


def send_message(message):
    """
    Sends a plain text message to Telegram.
    """
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id"   : CHAT_ID,
        "text"      : message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, data=data, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Telegram error: {e}")
        return None


def send_confirmation_buttons(trade_id, symbol, decision,
                               confidence, reason, amount):
    """
    Sends a trade confirmation message with YES/NO buttons.
    Agent waits for your tap before executing.
    """
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    message = f"""
🤖 <b>Trade Confirmation Required</b>

Token      : <b>{symbol}</b>
Decision   : <b>{decision}</b>
Confidence : <b>{confidence}</b>
Amount     : <b>${amount}</b>
Reason     : {reason}

Tap a button to confirm or skip:
"""

    # Inline keyboard buttons
    keyboard = {
        "inline_keyboard": [[
            {
                "text"         : "✅ CONFIRM",
                "callback_data": f"confirm_{trade_id}"
            },
            {
                "text"         : "❌ SKIP",
                "callback_data": f"skip_{trade_id}"
            }
        ]]
    }

    data = {
        "chat_id"     : CHAT_ID,
        "text"        : message,
        "parse_mode"  : "HTML",
        "reply_markup": requests.compat.json.dumps(keyboard)
    }

    try:
        response = requests.post(url, data=data, timeout=10)
        result   = response.json()

        if result.get("ok"):
            message_id = result["result"]["message_id"]
            # Store pending trade
            pending_trades[trade_id] = {
                "symbol"    : symbol,
                "decision"  : decision,
                "amount"    : amount,
                "status"    : "pending",
                "message_id": message_id
            }
            return message_id

    except Exception as e:
        print(f"Button message error: {e}")

    return None


def wait_for_confirmation(trade_id, timeout_seconds=120):
    """
    Waits for user to tap CONFIRM or SKIP button.
    Times out after 2 minutes if no response.
    Returns True if confirmed, False if skipped or timed out.
    """
    print(f"   Waiting for Telegram confirmation (2 min timeout)...")
    start_time  = time.time()
    last_update = 0

    while time.time() - start_time < timeout_seconds:

        # Check for button clicks via Telegram updates
        try:
            url      = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            params   = {"offset": last_update + 1, "timeout": 10}
            response = requests.get(url, params=params, timeout=15)
            updates  = response.json()

            if updates.get("ok") and updates.get("result"):
                for update in updates["result"]:
                    last_update = update["update_id"]

                    # Check if this is a button click
                    if "callback_query" in update:
                        callback = update["callback_query"]
                        data     = callback.get("data", "")
                        cb_id    = callback["id"]

                        # Answer the callback to remove loading state
                        answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
                        requests.post(answer_url, data={"callback_query_id": cb_id}, timeout=5)

                        # Check if this matches our trade
                        if f"confirm_{trade_id}" in data:
                            print(f"   User CONFIRMED trade!")
                            pending_trades[trade_id]["status"] = "confirmed"

                            # Update message to show confirmed
                            edit_message(
                                pending_trades[trade_id]["message_id"],
                                f"✅ <b>Trade CONFIRMED!</b>\nExecuting {pending_trades[trade_id]['symbol']} trade now..."
                            )
                            return True

                        elif f"skip_{trade_id}" in data:
                            print(f"   User SKIPPED trade!")
                            pending_trades[trade_id]["status"] = "skipped"

                            # Update message to show skipped
                            edit_message(
                                pending_trades[trade_id]["message_id"],
                                f"❌ <b>Trade SKIPPED</b>\n{pending_trades[trade_id]['symbol']} trade cancelled."
                            )
                            return False

        except Exception as e:
            print(f"   Polling error: {e}")

        time.sleep(3)

    # Timeout — auto skip
    print(f"   Confirmation timeout! Auto-skipping trade.")
    send_message(f"⏰ <b>Confirmation Timeout</b>\nNo response received. {pending_trades.get(trade_id, {}).get('symbol', '')} trade skipped.")
    return False


def edit_message(message_id, new_text):
    """
    Edits an existing Telegram message.
    Used to update confirmation buttons after user taps.
    """
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    data = {
        "chat_id"   : CHAT_ID,
        "message_id": message_id,
        "text"      : new_text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, data=data, timeout=10)
    except:
        pass


def notify_agent_started():
    msg = f"""
🤖 <b>Solana AI Trading Agent Started!</b>

🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔄 Scanning every 15 minutes
💰 Starting balance: $100.00 USDT
🌐 Chain: Solana
📊 Data: Bitget Wallet Skill API
🧠 AI: Groq Llama 3.3

⚠️ <b>Manual confirmation mode ON</b>
You will be asked before every trade!
"""
    return send_message(msg)


def notify_scan_complete(bullish_count, total_count):
    """Deprecated — use telegram_bot.notify_scan_complete instead."""
    pass



def notify_security_check(symbol, verdict, reason):
    if verdict == "SAFE":
        emoji = "✅"
    elif verdict == "WARNING":
        emoji = "⚠️"
    elif verdict == "DANGEROUS":
        emoji = "🚨"
    else:
        emoji = "❓"

    msg = f"""
{emoji} <b>Security Check: {symbol}</b>

Verdict : {verdict}
Reason  : {reason}
Source  : Bitget Wallet Skill API
"""
    return send_message(msg)


def notify_trade_executed(trade_type, symbol, amount_usdt,
                          price, tokens, balance):
    if trade_type == "BUY":
        emoji = "💚"
    else:
        emoji = "❤️"

    msg = f"""
{emoji} <b>Paper Trade Executed!</b>

Type    : {trade_type}
Token   : {symbol}
Amount  : ${amount_usdt}
Price   : ${price}
Tokens  : {tokens:.6f}
Balance : ${balance:.2f} USDT left
Via     : Bitget Wallet Skill API
Time    : {datetime.now().strftime('%H:%M:%S')}
"""
    return send_message(msg)


def notify_portfolio_status(balance, total_value,
                             profit_loss, total_trades):
    if profit_loss >= 0:
        pnl_text = f"+${profit_loss:.2f} 📈"
    else:
        pnl_text = f"-${abs(profit_loss):.2f} 📉"

    msg = f"""
📋 <b>Portfolio Update</b>

💵 USDT Balance : ${balance:.2f}
💼 Total Value  : ${total_value:.2f}
📊 P&L          : {pnl_text}
🔢 Total Trades : {total_trades}
🕐 Time         : {datetime.now().strftime('%H:%M:%S')}
"""
    return send_message(msg)


def notify_no_trades(reason):
    msg = f"""
😴 <b>No Trades This Cycle</b>

Reason : {reason}
Time   : {datetime.now().strftime('%H:%M:%S')}
Next scan in 15 minutes...
"""
    return send_message(msg)


def notify_error(error_message):
    msg = f"""
❌ <b>Agent Error</b>

Error : {error_message}
Time  : {datetime.now().strftime('%H:%M:%S')}
Agent will retry in 15 minutes...
"""
    return send_message(msg)


def notify_ai_decision(symbol, decision, confidence, reason, amount):
    if decision == "BUY":
        emoji = "🟢"
    elif decision == "SELL":
        emoji = "🔴"
    else:
        emoji = "🟡"

    msg = f"""
{emoji} <b>AI Trading Decision</b>

Token      : {symbol}
Decision   : {decision}
Confidence : {confidence}
Reason     : {reason}
Amount     : ${amount}
AI Model   : Groq Llama 3.3
"""
    return send_message(msg)


def notify_stop_loss(symbol, reason, pct_change,
                     buy_price, sell_price, balance):
    """
    Sends stop-loss or take-profit notification.
    """
    if reason == "STOP_LOSS":
        emoji  = "🛑"
        title  = "Stop-Loss Triggered!"
        color  = "Loss"
    else:
        emoji  = "💰"
        title  = "Take-Profit Triggered!"
        color  = "Profit"

    msg = f"""
{emoji} <b>{title}</b>

Token      : {symbol}
Reason     : {reason}
Change     : {pct_change}%
Buy Price  : ${buy_price}
Sell Price : ${sell_price}
Balance    : ${balance:.2f} USDT
Time       : {datetime.now().strftime('%H:%M:%S')}

Position automatically closed!
"""
    return send_message(msg)
# Test
if __name__ == "__main__":
    import json as json_lib

    print("Testing confirmation buttons...")

    trade_id  = "test_001"
    msg_id    = send_confirmation_buttons(
        trade_id   = trade_id,
        symbol     = "Fartcoin",
        decision   = "BUY",
        confidence = "HIGH",
        reason     = "Strong momentum with high volume",
        amount     = "10"
    )

    if msg_id:
        print("Confirmation message sent! Check Telegram and tap a button...")
        result = wait_for_confirmation(trade_id, timeout_seconds=60)
        print(f"Result: {'CONFIRMED' if result else 'SKIPPED'}")
    else:
        print("Failed to send confirmation message")