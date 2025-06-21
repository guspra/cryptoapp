from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import ccxt
import os
import random
import websocket
import json
import uuid
import time

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret-crypto-app!"  # Secret key for session management
# Use eventlet for async mode, which is required for background threads
socketio = SocketIO(app, async_mode="eventlet")
ws_thread = None  # Global variable to hold the background thread

# In-memory store for verification data instead of session
# Format: {request_id: {"code": "1234", "order_data": {...}, "timestamp": 12345.67}}
verification_data_store = {}

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
    # --- 1. Initial page load (no prices fetched here) ---
    # Prices will be fetched asynchronously by JavaScript after page load.

    # --- Check for keys, but DO NOT fetch balances here ---
    api_key = os.environ.get("BINANCE_API_KEY")
    secret_key = os.environ.get("BINANCE_SECRET_KEY")
    keys_set = bool(api_key and secret_key)

    return render_template(
        "index.html",
        keys_set=keys_set,
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

        if not all([symbol, order_type, side, amount]):
            return (
                jsonify(
                    {"success": False, "error": "Missing required order parameters."}
                ),
                400,
            )

        # --- Verification System ---
        # Generate a unique ID for this verification attempt
        request_id = str(uuid.uuid4())
        verification_code = str(random.randint(1000, 9999))

        # Store the data on the server, linked to the unique request_id
        verification_data_store[request_id] = {
            "code": verification_code,
            "order_data": data,
            "timestamp": time.time(),
        }

        # Send WhatsApp message (replace with your Twilio credentials)
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        twilio_from = os.environ.get("TWILIO_WHATSAPP_FROM")
        twilio_to = os.environ.get("TWILIO_WHATSAPP_TO")  # Your number from env

        if not all([account_sid, auth_token, twilio_from, twilio_to]):
            return (
                jsonify(
                    {"success": False, "error": "Twilio credentials not configured."}
                ),
                500,
            )

        from twilio.rest import Client

        client = Client(account_sid, auth_token)

        message = client.messages.create(
            from_=twilio_from,
            body=f"Your verification code: {verification_code}",
            to=twilio_to,
        )

        print(
            f"Sent verification code via WhatsApp. Message SID: {message.sid}"
        )  # for logging

        return (
            jsonify(
                {
                    "success": True,
                    "verification_required": True,
                    "request_id": request_id,
                }
            ),
            200,
        )
    except Exception as e:
        return jsonify({"success": False, "error": f"An error occurred: {str(e)}"}), 500


@app.route("/verify_order", methods=["POST"])
def verify_order():
    """Verifies the order with the code entered by the user and places the order"""
    api_key = os.environ.get("BINANCE_API_KEY")
    secret_key = os.environ.get("BINANCE_SECRET_KEY")

    if not api_key or not secret_key:
        return (
            jsonify({"success": False, "error": "API keys not configured on server."}),
            403,
        )

    data = request.get_json()
    request_id = data.get("request_id")
    entered_code = data.get("code")

    if not request_id:
        return jsonify({"success": False, "error": "Missing request ID."}), 400

    # Retrieve and remove the stored data to prevent reuse
    stored_data = verification_data_store.pop(request_id, None)

    if not stored_data:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "No order to verify. It may have expired or already been used.",
                }
            ),
            400,
        )

    # Check for expiry (e.g., 5 minutes)
    if time.time() - stored_data["timestamp"] > 300:
        return (
            jsonify({"success": False, "error": "Verification code has expired."}),
            400,
        )

    if entered_code != stored_data["code"]:
        return jsonify({"success": False, "error": "Invalid verification code."}), 401

    try:
        order_data = stored_data["order_data"]
        symbol = order_data.get("symbol")
        order_type = order_data.get("type")
        side = order_data.get("side")
        amount = order_data.get("amount")
        price = order_data.get("price")

        exchange = ccxt.binance({"apiKey": api_key, "secret": secret_key})

        order = None
        if order_type == "market":
            order = exchange.create_order(
                symbol, "market", side, amount=None, params={"quoteOrderQty": amount}
            )
        elif order_type == "limit":
            if not price or price <= 0:
                return (
                    jsonify(
                        {"success": False, "error": "Limit price must be positive."}
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


@app.route("/api/balances")
def get_balances():
    """A dedicated API endpoint to fetch wallet balances asynchronously."""
    api_key = os.environ.get("BINANCE_API_KEY")
    secret_key = os.environ.get("BINANCE_SECRET_KEY")

    if not api_key or not secret_key:
        return (
            jsonify({"success": False, "error": "API keys not configured on server."}),
            403,
        )

    try:
        auth_exchange = ccxt.binance({"apiKey": api_key, "secret": secret_key})

        spot_balance = auth_exchange.fetch_balance()
        spot_assets = {
            asset: balance["total"]
            for asset, balance in spot_balance.items()
            if isinstance(balance, dict) and "total" in balance and balance["total"] > 0
        }

        futures_balance = auth_exchange.fetch_balance(params={"type": "future"})
        futures_assets = {
            asset: balance["total"]
            for asset, balance in futures_balance.items()
            if isinstance(balance, dict) and "total" in balance and balance["total"] > 0
        }

        return jsonify(
            {
                "success": True,
                "spot_assets": spot_assets,
                "futures_assets": futures_assets,
            }
        )

    except ccxt.AuthenticationError:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Authentication Error. Please check your API keys.",
                }
            ),
            401,
        )
    except Exception as e:
        return (
            jsonify({"success": False, "error": f"Error fetching balances: {e}"}),
            500,
        )


@app.route("/api/prices")
def get_prices_api():
    """A dedicated API endpoint to fetch market prices asynchronously."""
    try:
        public_exchange = ccxt.binance()
        symbols_to_fetch = ["BTC/USDT", "ETH/USDT", "DOGE/USDT"]
        tickers = public_exchange.fetch_tickers(symbols_to_fetch)
        prices = [
            {"symbol": ticker["symbol"], "price": ticker["last"]}
            for symbol, ticker in tickers.items()
        ]
        return jsonify({"success": True, "prices": prices})
    except Exception as e:
        return (
            jsonify({"success": False, "error": f"Could not fetch market prices: {e}"}),
            500,
        )


@socketio.on("connect")
def handle_connect():
    """Starts the Binance WebSocket client when the first user connects."""
    global ws_thread
    if ws_thread is None:
        print("Client connected, starting Binance WebSocket background task.")
        # Use socketio.start_background_task for compatibility with eventlet
        ws_thread = socketio.start_background_task(target=binance_ws_client)


@socketio.on("disconnect")
def handle_disconnect():
    """Logs when a client disconnects."""
    print("Client disconnected.")


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

    while True:
        try:
            ws = websocket.WebSocketApp(
                socket_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever()
        except Exception as e:
            print(f"WebSocket client encountered an error: {e}")
        print("WebSocket connection closed. Reconnecting in 5 seconds...")
        time.sleep(5)


if __name__ == "__main__":
    # Run the Flask-SocketIO server
    # The background task will be started on the first client connection.
    socketio.run(app, host="0.0.0.0", port=5000)
