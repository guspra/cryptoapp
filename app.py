from flask import Flask, render_template
import ccxt
import os

app = Flask(__name__)


@app.route("/")
def index():
    # --- 1. Fetch Public Market Prices (No Auth Needed) ---
    prices = []
    price_error = None
    try:
        public_exchange = ccxt.binance()
        symbols_to_fetch = ["BTC/USDT", "ETH/USDT", "DOGE/USDT"]
        tickers = public_exchange.fetch_tickers(symbols_to_fetch)
        prices = [
            {"symbol": ticker["symbol"], "price": ticker["last"]}
            for ticker in tickers.values()
        ]
    except Exception as e:
        price_error = f"Could not fetch market prices: {e}"

    # --- 2. Fetch Private Balances (Auth Needed) ---
    api_key = os.environ.get("BINANCE_API_KEY")
    secret_key = os.environ.get("BINANCE_SECRET_KEY")
    keys_set = bool(api_key and secret_key)
    spot_assets, futures_assets, balance_error = {}, {}, None

    if keys_set:
        try:
            # Initialize the exchange with authentication credentials
            auth_exchange = ccxt.binance(
                {
                    "apiKey": api_key,
                    "secret": secret_key,
                }
            )

            # Fetch Spot Balance
            spot_balance = auth_exchange.fetch_balance()
            spot_assets = {
                asset: balance["total"]
                for asset, balance in spot_balance.items()
                if isinstance(balance, dict)
                and "total" in balance
                and balance["total"] > 0
            }

            # Fetch Futures Balance
            futures_balance = auth_exchange.fetch_balance(params={"type": "future"})
            futures_assets = {
                asset: balance["total"]
                for asset, balance in futures_balance.items()
                if isinstance(balance, dict)
                and "total" in balance
                and balance["total"] > 0
            }

        except ccxt.AuthenticationError:
            balance_error = "Authentication Error. Please check your API keys."
        except Exception as e:
            balance_error = f"Error fetching balances: {e}"

    return render_template(
        "index.html",
        prices=prices,
        price_error=price_error,
        keys_set=keys_set,
        spot_assets=spot_assets,
        futures_assets=futures_assets,
        balance_error=balance_error,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
