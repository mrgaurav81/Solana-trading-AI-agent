# Solana AI Trading Agent 🤖🔥

A highly intelligent, automated trading agent for Solana meme-coins and tokens. This project was built for the **Bitget Wallet Hackathon 2026** and utilizes the power of **Groq Llama 3.3** for lightning-fast decision-making and the **Bitget Wallet Skill API** for robust on-chain security, price fetching, and trading execution.

## 🌟 Features

* **Smart AI Brain**: Uses Groq (Llama 3.3) to analyze market trends, volume shifts, and momentum to select winning strategies (`MOMENTUM`, `DCA`, `BREAKOUT`).
* **Advanced Security & Anti-Rug**: Integrates with the Bitget Wallet Skill API to aggressively filter out rugs, honeypots, and malicious contracts before any trade is executed. 
* **Telegram Bot Integration**: Full remote control of your agent! Switch between `/auto` and `/manual` modes. In manual mode, the agent pushes interactive trade requests to your Telegram asking for immediate Yes/No confirmations.
* **Paper Trading & Portfolio Tracking**: Built-in risk-free paper trading engine (`paper_trader.py`), complete with Stop-Loss and Take-Profit mechanics.
* **Local Dashboard Server**: Real-time visualization of your agent's performance, P&L, and open holdings via an interactive web dashboard.

## 🛠️ Architecture

* `main_agent.py` - Core execution loop and orchestrator.
* `ai_brain.py` - Interfaces with Groq AI to process market signals and return exact trading decisions.
* `bitget_skill.py` - Wraps Bitget Wallet Skill APIs for on-chain interactions, token validation, and deep liquidity checks.
* `dashboard_server.py` - Local UI to monitor active trades and total P&L.
* `telegram_bot.py` / `telegram_notify.py` - Two-way communication channel with Telegram for alerts and remote execution.

## 🚀 Setup & Installation

**1. Clone the repository**
```bash
git clone https://github.com/YourUsername/Solana-trading-AI-agent.git
cd Solana-trading-AI-agent
```

**2. Create a virtual environment & install dependencies**
Ensure you have Python 3.9+ installed.
```bash
python -m venv venv
source venv/bin/activate  # On Windows use: venv\Scripts\activate
pip install -r requirements.txt
pip install python-dotenv groq requests
```

**3. Environment Variables**
Create a `.env` file in the root of the project with your API keys:
```env
GROQ_API_KEY="your_groq_api_key_here"
TELEGRAM_BOT_TOKEN="your_telegram_bot_token_here"
```

**4. Run the Agent**
You can run the core loop directly, or start the Telegram Bot to manage it remotely.
```bash
python main_agent.py
```
To launch the dashboard server visualization:
```bash
python dashboard_server.py
```

## 🎮 Telegram Bot Commands

Once your Telegram bot is running, message it to take control:
* `/start` - Boot up the AI agent.
* `/stop` - Immediately halt all background execution.
* `/auto` - Switch to fully autonomous trading.
* `/manual` - Switch to manual confirmation mode (the bot will message you before entering any trade).
* `/status` - View current USDT balance, Total Value, and P&L.
* `/holdings` - View detailed breakdowns of all active positions.

## 🏆 Hackathon Submission
Built for the Bitget Wallet Hackathon 2026. This agent is designed to bridge the gap between large language models and volatile on-chain economies safely. By leveraging the Bitget Wallet Skill API's robust token verification endpoints, the AI can embrace calculated risks without falling victim to simple honeypots.

---
*Disclaimer: This software is for educational and hackathon purposes. Cryptocurrency trading carries significant risk. Always test with paper trading before risking real capital.*
