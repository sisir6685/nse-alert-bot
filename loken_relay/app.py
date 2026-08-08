"""
LOKEN Buy/Sell -> Telegram relay
--------------------------------
Receives TradingView webhook alerts (JSON payloads produced by the
alert() calls in LOKEN_Buy_Sell_Combined.pine) and forwards a formatted
message to a Telegram chat via the Bot API.

Environment variables required (set these in Render's dashboard, reusing
the same bot token / chat ID as the existing NSE Dashboard project):
    TELEGRAM_BOT_TOKEN   - your bot's token from @BotFather
    TELEGRAM_CHAT_ID     - the chat/channel/group ID to post into
    WEBHOOK_SECRET       - (optional but recommended) a shared secret;
                           TradingView must include this in the alert
                           message JSON as "secret" for the request to
                           be accepted. Prevents randoms from spamming
                           your Telegram via the public webhook URL.

Endpoints:
    GET  /            - health check (returns 200 OK, useful for
                         Render's health checks and for confirming the
                         service is live before wiring up TradingView)
    POST /webhook      - the URL to paste into TradingView's alert
                         "Webhook URL" field
"""

import os
import json
import logging
from datetime import datetime

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loken-relay")

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Buy signals get a green circle, Sell signals get a red circle, purely
# for quick visual scanning in the Telegram chat.
def _emoji_for_signal(signal_name: str) -> str:
    if signal_name.lower().startswith("buy"):
        return "🟢"
    if signal_name.lower().startswith("sell"):
        return "🔴"
    return "⚪️"


def _format_message(payload: dict) -> str:
    signal = payload.get("signal", "Unknown Signal")
    ticker = payload.get("ticker", "?")
    exchange = payload.get("exchange", "")
    price = payload.get("price", "?")
    time_str = payload.get("time", "")

    emoji = _emoji_for_signal(signal)
    exch_part = f"{exchange}:" if exchange else ""

    lines = [
        f"{emoji} <b>{signal}</b>",
        f"<b>{exch_part}{ticker}</b>",
        f"Price: {price}",
    ]
    if time_str:
        lines.append(f"Time: {time_str}")
    lines.append(f"Received: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return "\n".join(lines)


def _send_to_telegram(text: str) -> tuple[bool, str]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured"
    try:
        resp = requests.post(
            TELEGRAM_API_URL,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "ok"
        return False, f"Telegram API error {resp.status_code}: {resp.text}"
    except requests.RequestException as exc:
        return False, f"Request to Telegram failed: {exc}"


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "loken-telegram-relay"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body = request.get_data(as_text=True)
    log.info("Incoming webhook, raw body: %s", raw_body)

    # TradingView sends the alert message as the raw request body. It's
    # usually valid JSON (since our Pine alert() calls build JSON
    # strings), but TradingView may wrap/escape it depending on settings,
    # so we try a couple of parsing strategies before giving up.
    payload = None
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        try:
            payload = request.get_json(force=True, silent=True)
        except Exception:
            payload = None

    if not isinstance(payload, dict):
        log.warning("Could not parse payload as JSON: %s", raw_body)
        return jsonify({"status": "error", "reason": "invalid JSON payload"}), 400

    if WEBHOOK_SECRET:
        if payload.get("secret") != WEBHOOK_SECRET:
            log.warning("Rejected webhook: bad or missing secret")
            return jsonify({"status": "error", "reason": "unauthorized"}), 401

    message_text = _format_message(payload)
    ok, detail = _send_to_telegram(message_text)

    if not ok:
        log.error("Failed to relay to Telegram: %s", detail)
        return jsonify({"status": "error", "reason": detail}), 502

    log.info("Relayed signal to Telegram: %s", payload.get("signal"))
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
