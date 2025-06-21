from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import ccxt
import os
import threading
import websocket
import json

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret-crypto-app!"  # Secret key for session management
# Use eventlet for async mode, which is required for background threads
socketio = SocketIO(app, async_mode="eventlet")
ws_thread = None  # Global variable to hold the background thread

PRICE_PRECISION = {
    "BTC/USDT": 2,
    "ETH/USDT": 4,
    "DOGE/USDT": 7,
    "BTCUSDT": 2,
    "ETHUSDT": 4,
    "DOGEUSDT": 7,
}


@app.template_filter("format_price")
def format_price_filter(price, symbol):
    """Custom Jinja filter to format prices with symbol-specific precision."""
    precision = PRICE_PRECISION.get(symbol, 2)
    # Format with commas and the specified number of decimal places
    return f"{price:,.{precision}f}"


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
            for symbol, ticker in tickers.items()
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
        "index.html",  # This will now render the entire page including JS
        prices=prices,
        price_error=price_error,
        keys_set=keys_set,
        spot_assets=spot_assets,
        futures_assets=futures_assets,
        balance_error=balance_error,
    )


@app.route("/place_order", methods=["POST"])
def place_order():
    """Receives order data from the frontend and places it via ccxt."""
    api_key = os.environ.get("BINANCE_API_KEY")
    secret_key = os.environ.get("BINANCE_SECRET_KEY")

    if not api_key or not secret_key:
        return (
            jsonify({"success": False, "error": "API keys not configured on server."}),
            403,
        )

    try:
        data = request.get_json()
        symbol = data.get("symbol")
        order_type = data.get("type")
        side = data.get("side")
        amount = data.get("amount")
        price = data.get("price")  # Can be None for market orders

        if not all([symbol, order_type, side, amount]):
            return (
                jsonify(
                    {"success": False, "error": "Missing required order parameters."}
                ),
                400,
            )

        # Initialize authenticated exchange
        exchange = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": secret_key,
            }
        )

        # Create the order
        order = None
        if order_type == "market":
            # For a market buy, the user specifies the cost in quote currency (USDT)
            order = exchange.create_order(
                symbol,
                "market",
                side,
                amount=None,  # Amount in base currency is unknown
                params={"quoteOrderQty": amount},  # Specify cost in quote currency
            )
        elif order_type == "limit":
            # For a limit buy, user specifies cost in USDT and a limit price.
            # We must calculate the amount in base currency (BTC).
            if not price or price <= 0:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Limit price must be a positive number.",
                        }
                    ),
                    400,
                )
            btc_amount = amount / price
            order = exchange.create_order(symbol, "limit", side, btc_amount, price)

        return (
            jsonify({"success": True, "order": order})
            if order
            else jsonify({"success": False, "error": "Unsupported order type"})
        ), 400

    except ccxt.InsufficientFunds as e:
        return jsonify({"success": False, "error": f"Insufficient funds: {e}"}), 400
    except ccxt.InvalidOrder as e:
        return jsonify({"success": False, "error": f"Invalid order: {e}"}), 400
    except ccxt.AuthenticationError:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Authentication failed. Check your API keys.",
                }
            ),
            401,
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@socketio.on("connect")
def handle_connect():
    """Starts the Binance WebSocket client when the first user connects."""
    global ws_thread
    if ws_thread is None:
        print("Client connected, starting Binance WebSocket background task.")
        # Use socketio.start_background_task for compatibility with eventlet
        ws_thread = socketio.start_background_task(target=binance_ws_client)


def binance_ws_client():
    """Connects to Binance WebSocket stream and emits price updates."""
    symbols = ["btcusdt", "ethusdt", "dogeusdt"]
    streams = "/".join([f"{s}@trade" for s in symbols])
    socket_url = f"wss://stream.binance.com:9443/stream?streams={streams}"

    def on_message(ws, message):
        data = json.loads(message)
        if "stream" in data and "data" in data:
            payload = data["data"]
            symbol = payload["s"]
            price = float(payload["p"])
            # Emit a 'price_update' event to all connected clients
            socketio.emit("price_update", {"symbol": symbol, "price": price})

    def on_error(ws, error):
        print(f"WebSocket Error: {error}")

    def on_close(ws, close_status_code, close_msg):
        print("### WebSocket closed ###")

    def on_open(ws):
        print("### WebSocket connection opened ###")

    ws = websocket.WebSocketApp(
        socket_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever()


if __name__ == "__main__":
    # Run the Flask-SocketIO server
    # The background task will be started on the first client connection.
    socketio.run(app, host="0.0.0.0", port=5000)
