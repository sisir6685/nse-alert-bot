"""
LOKEN Buy/Sell -> Telegram relay
--------------------------------
Receives TradingView webhook alerts (JSON payloads produced by the
alert() calls in LOKEN_Buy_Sell_Combined.pine) and:
  1) forwards a formatted message to a Telegram chat via the Bot API
  2) (during NSE market hours only) appends a row to a signal_log.csv
     file kept in this GitHub repo, via the GitHub Contents API - since
     Render's free tier does not persist local files across restarts,
     GitHub itself is the durable storage here, same pattern the
     existing nse-alert-bot code already uses for its own log.

Environment variables required:
    TELEGRAM_BOT_TOKEN   - your bot's token from @BotFather
    TELEGRAM_CHAT_ID     - the chat/channel/group ID to post into
    WEBHOOK_SECRET       - (optional but recommended) shared secret;
                           TradingView must include this in the alert
                           message JSON as "secret" for the request to
                           be accepted.

Environment variables for the CSV logging feature (optional - if
GITHUB_TOKEN is not set, logging is skipped entirely and Telegram
delivery still works normally):
    GITHUB_TOKEN         - a GitHub Personal Access Token with
                           "repo" (or fine-grained "Contents: write")
                           permission on the target repo
    GITHUB_REPO          - "owner/repo", e.g. "sisir6685/nse-alert-bot"
    GITHUB_LOG_PATH       - path to the CSV file within that repo,
                           defaults to "loken_relay/signal_log.csv"
    GITHUB_BRANCH        - defaults to "main"

Endpoints:
    GET  /            - health check
    POST /webhook      - the URL to paste into TradingView's alert
                         "Webhook URL" field
"""

import os
import json
import base64
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loken-relay")

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_LOG_PATH = os.environ.get("GITHUB_LOG_PATH", "loken_relay/signal_log.csv")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)   # 9:15 AM IST
MARKET_CLOSE = (15, 30)  # 3:30 PM IST
# FIX: dropped the "alert_time" column (the bar-close time embedded in
# the Pine script's message). It was redundant with timestamp_ist (the
# relay's own receipt time, first column) and usually only a few seconds
# apart from it, so it added noise without adding information.
CSV_HEADER = "timestamp_ist,signal,ticker,exchange,price\n"


def _emoji_for_signal(signal_name: str) -> str:
    if signal_name.lower().startswith("buy"):
        return "🟢"
    if signal_name.lower().startswith("sell"):
        return "🔴"
    return "⚪️"


def _format_message(payload: dict) -> str:
    """FIX: trimmed to exactly ticker, signal type, price, and time —
    dropped the separate exchange line and the "Received: UTC" footer,
    which were extra clutter beyond what was asked for. Now two compact
    lines instead of four."""
    signal = payload.get("signal", "Unknown Signal")
    ticker = payload.get("ticker", "?")
    exchange = payload.get("exchange", "")
    price = payload.get("price", "?")
    time_str = payload.get("time", "")

    emoji = _emoji_for_signal(signal)
    exch_part = f"{exchange}:" if exchange else ""

    line1 = f"{emoji} <b>{signal}</b> — {exch_part}{ticker}"
    line2 = f"₹{price}"
    if time_str:
        line2 += f"  |  {time_str}"

    return line1 + "\n" + line2


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


def _now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def _is_market_hours(now_ist: datetime) -> bool:
    if now_ist.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_t = now_ist.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_t = now_ist.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_t <= now_ist <= close_t


def _append_to_github_csv(payload: dict) -> tuple[bool, str]:
    """Appends one row to the CSV file kept in the GitHub repo, via the
    GitHub Contents API (fetch current file + sha, append a row, commit
    back). Skipped entirely if GITHUB_TOKEN/GITHUB_REPO aren't set, or if
    it's currently outside NSE market hours."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False, "GITHUB_TOKEN / GITHUB_REPO not configured - logging disabled"

    now_ist = _now_ist()
    if not _is_market_hours(now_ist):
        return False, "outside market hours - skipped (this is expected, not an error)"

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_LOG_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    try:
        get_resp = requests.get(f"{api_url}?ref={GITHUB_BRANCH}", headers=headers, timeout=10)
    except requests.RequestException as exc:
        return False, f"GitHub GET request failed: {exc}"

    if get_resp.status_code == 200:
        file_data = get_resp.json()
        sha = file_data["sha"]
        try:
            current_content = base64.b64decode(file_data["content"]).decode("utf-8")
        except Exception:
            current_content = CSV_HEADER
    elif get_resp.status_code == 404:
        sha = None
        current_content = CSV_HEADER
    else:
        return False, f"GitHub GET failed {get_resp.status_code}: {get_resp.text}"

    # FIX: dropped the trailing payload.get("time", "") field — matches
    # the CSV_HEADER change above (alert_time column removed).
    row = "{},{},{},{},{}\n".format(
        now_ist.strftime("%Y-%m-%d %H:%M:%S"),
        payload.get("signal", ""),
        payload.get("ticker", ""),
        payload.get("exchange", ""),
        payload.get("price", ""),
    )
    new_content = current_content + row

    commit_payload = {
        "message": f"Log signal: {payload.get('signal', '?')} {payload.get('ticker', '?')} [skip ci]",
        "content": base64.b64encode(new_content.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        commit_payload["sha"] = sha

    try:
        put_resp = requests.put(api_url, headers=headers, json=commit_payload, timeout=15)
    except requests.RequestException as exc:
        return False, f"GitHub PUT request failed: {exc}"

    if put_resp.status_code in (200, 201):
        return True, "logged"
    return False, f"GitHub PUT failed {put_resp.status_code}: {put_resp.text}"


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "loken-telegram-relay"}), 200


def _parse_delimited_message(raw_body: str) -> dict | None:
    """Parses the "TICKER - SIGNAL - EXCHANGE - PRICE - TIME[ - SECRET]"
    format sent by the Pine script's alert() calls. Returns None if the
    text doesn't look like this format at all."""
    parts = [p.strip() for p in raw_body.strip().split(" - ")]
    if len(parts) < 5:
        return None
    ticker, signal, exchange, price, time_str = parts[0], parts[1], parts[2], parts[3], parts[4]
    secret = parts[5] if len(parts) >= 6 else None
    result = {
        "ticker": ticker,
        "signal": signal,
        "exchange": exchange,
        "price": price,
        "time": time_str,
    }
    if secret is not None:
        result["secret"] = secret
    return result


@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body = request.get_data(as_text=True)
    log.info("Incoming webhook, raw body: %s", raw_body)

    # Try the human-readable pipe-delimited format first (this is what
    # the Pine script sends, so it also displays nicely in TradingView's
    # own Alert Log/toast notifications). Fall back to JSON for backward
    # compatibility with any older alert setups still using that format.
    payload = _parse_delimited_message(raw_body)
    if payload is None:
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            try:
                payload = request.get_json(force=True, silent=True)
            except Exception:
                payload = None

    if not isinstance(payload, dict):
        log.warning("Could not parse payload (tried delimited format and JSON): %s", raw_body)
        return jsonify({"status": "error", "reason": "unparseable payload"}), 400

    if WEBHOOK_SECRET:
        if payload.get("secret") != WEBHOOK_SECRET:
            log.warning("Rejected webhook: bad or missing secret")
            return jsonify({"status": "error", "reason": "unauthorized"}), 401

    message_text = _format_message(payload)
    telegram_ok, telegram_detail = _send_to_telegram(message_text)

    if not telegram_ok:
        log.error("Failed to relay to Telegram: %s", telegram_detail)
        return jsonify({"status": "error", "reason": telegram_detail}), 502

    log.info("Relayed signal to Telegram: %s", payload.get("signal"))

    # CSV logging never blocks or fails the Telegram delivery above -
    # it's best-effort, logged either way.
    csv_ok, csv_detail = _append_to_github_csv(payload)
    if csv_ok:
        log.info("Logged signal to GitHub CSV: %s", payload.get("signal"))
    else:
        log.info("Skipped/failed GitHub CSV logging: %s", csv_detail)

    return jsonify({"status": "ok", "telegram": "sent", "csv_log": "written" if csv_ok else csv_detail}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
