
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
import os

load_dotenv()

client = TradingClient(
    api_key=os.environ["ALPACA_API_KEY"],
    secret_key=os.environ["ALPACA_SECRET_KEY"],
    paper=True,
)

account = client.get_account()

print("Paper account connected successfully")
print(f"Status: {account.status}")
print(f"Equity: ${account.equity}")
print(f"Cash: ${account.cash}")
