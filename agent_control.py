import json
import os
import time
from datetime import datetime

CONTROL_FILE  = "control.json"
PENDING_FILE  = "pending_trades.json"


def get_control():
    """
    Reads current agent control state.
    """
    defaults = {
        "running"    : False,
        "mode"       : "manual",
        "updated_at" : "",
        "started_at" : "",
        "stopped_at" : ""
    }
    if os.path.exists(CONTROL_FILE):
        try:
            with open(CONTROL_FILE, "r") as f:
                saved = json.load(f)
                defaults.update(saved)
        except:
            pass
    return defaults


def set_control(running, mode=None):
    """
    Updates agent control state.
    """
    ctrl = get_control()
    ctrl["running"]    = running
    ctrl["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if mode:
        ctrl["mode"] = mode

    if running:
        ctrl["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        ctrl["stopped_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(CONTROL_FILE, "w") as f:
        json.dump(ctrl, f, indent=2)


def is_running():
    return get_control().get("running", False)


def get_mode():
    return get_control().get("mode", "manual")


def start_agent():
    set_control(True)


def stop_agent():
    set_control(False)


def set_mode(mode):
    ctrl = get_control()
    ctrl["mode"]       = mode
    ctrl["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CONTROL_FILE, "w") as f:
        json.dump(ctrl, f, indent=2)


# ── Pending trades ────────────────────────────────────────────────

def add_pending_trade(trade_id, symbol, decision,
                      confidence, reason, amount, strategy=""):
    """
    Adds trade to pending queue for dashboard/Telegram confirmation.
    """
    trades = load_pending_trades()
    trades[trade_id] = {
        "trade_id"  : trade_id,
        "symbol"    : symbol,
        "decision"  : decision,
        "confidence": confidence,
        "reason"    : reason,
        "amount"    : amount,
        "strategy"  : strategy,
        "status"    : "pending",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": int(time.time()) + 120
    }
    save_pending_trades(trades)
    return trade_id


def get_pending_trades():
    """
    Returns active pending trades, removes expired ones.
    """
    trades = load_pending_trades()
    now    = int(time.time())
    active = {}

    for tid, trade in trades.items():
        if trade.get("status") == "pending":
            if trade.get("expires_at", 0) > now:
                active[tid] = trade
            else:
                trades[tid]["status"] = "expired"

    save_pending_trades(trades)
    return active


def confirm_trade(trade_id):
    trades = load_pending_trades()
    if trade_id in trades:
        trades[trade_id]["status"]       = "confirmed"
        trades[trade_id]["confirmed_at"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        save_pending_trades(trades)
        return True
    return False


def skip_trade(trade_id):
    trades = load_pending_trades()
    if trade_id in trades:
        trades[trade_id]["status"]    = "skipped"
        trades[trade_id]["skipped_at"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        save_pending_trades(trades)
        return True
    return False


def wait_for_decision(trade_id, timeout=120):
    """
    Waits for user to confirm/skip from dashboard OR Telegram.
    """
    start = time.time()
    while time.time() - start < timeout:
        trades = load_pending_trades()
        trade  = trades.get(trade_id, {})
        status = trade.get("status", "pending")

        if status == "confirmed":
            return True
        elif status in ["skipped", "expired"]:
            return False
        time.sleep(2)

    skip_trade(trade_id)
    return False


def load_pending_trades():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_pending_trades(trades):
    with open(PENDING_FILE, "w") as f:
        json.dump(trades, f, indent=2)
        