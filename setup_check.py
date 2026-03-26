# setup_check.py — confirms everything is installed and working

import sys
import requests
from solders.keypair import Keypair
from dotenv import load_dotenv

print("=" * 50)
print("  Solana AI Trading Agent — Setup Check")
print("=" * 50)

# 1. Python version
print(f"\n✅ Python version: {sys.version}")

# 2. Generate a brand new Solana wallet
keypair = Keypair()
wallet_address = str(keypair.pubkey())
private_key = keypair.secret().hex()

print(f"\n✅ Solana wallet generated!")
print(f"   Public address : {wallet_address}")
print(f"   Private key    : {private_key}")
print(f"\n   ⚠️  SAVE YOUR PRIVATE KEY SOMEWHERE SAFE.")
print(f"       Never share it. Never commit it to GitHub.")

# 3. Check internet + Solana RPC connection
print("\n🔍 Testing Solana RPC connection...")
try:
    response = requests.post(
        "https://api.mainnet-beta.solana.com",
        json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        timeout=10
    )
    data = response.json()
    if data.get("result") == "ok":
        print("✅ Solana RPC is reachable!")
    else:
        print(f"⚠️  RPC responded but returned: {data}")
except Exception as e:
    print(f"❌ Could not reach Solana RPC: {e}")

# 4. Check Bitget market API
print("\n🔍 Testing Bitget market data API...")
try:
    r = requests.get(
        "https://api.bitget.com/api/v2/spot/market/tickers",
        params={"symbol": "SOLUSDT"},
        timeout=10
    )
    ticker = r.json()
    price = ticker["data"][0]["lastPr"]
    print(f"✅ Bitget API reachable! SOL price right now: ${float(price):.2f}")
except Exception as e:
    print(f"❌ Could not reach Bitget API: {e}")

print("\n" + "=" * 50)
print("  Setup complete! Ready to build the agent.")
print("=" * 50)
