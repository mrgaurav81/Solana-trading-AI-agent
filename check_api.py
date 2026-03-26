import requests
import json

response = requests.get(
    "https://api.bitget.com/api/v2/spot/market/tickers",
    params={"symbol": "SOLUSDT"},
    timeout=10
)

data = response.json()

# This will print ALL the field names Bitget actually sends us
print(json.dumps(data["data"][0], indent=2))

'''
Run it:
python check_api.py
'''