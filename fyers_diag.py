"""
Fyers API — Stage 1 diagnostic script
======================================
Purpose: verify the automated (TOTP + PIN, no browser) Fyers login flow
works end-to-end, and dump one COMPLETE raw option-chain response so we
can confirm the real response schema (especially where the underlying
spot/LTP lives) before writing the actual signal-detection logic.

This does NOT send Telegram alerts or evaluate any signals yet — it is
purely a one-shot diagnostic to unblock the next step.

Required environment variables (set as GitHub Actions secrets):
  FYERS_CLIENT_ID    e.g. "FAA03271"
  FYERS_PIN          your 4-digit trading PIN
  FYERS_TOTP_KEY     the TOTP secret key from Fyers' 2FA setup screen
  FYERS_APP_ID       e.g. "XXXXXXX-100"
  FYERS_SECRET_ID    the app's Secret ID
  FYERS_REDIRECT_URI e.g. "http://127.0.0.1:8080" (whatever you set on the app)
"""

import os
import base64
import hmac
import json
import struct
import time
from urllib.parse import urlparse, parse_qs

import requests
from fyers_apiv3 import fyersModel

FYERS_CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "")
FYERS_PIN = os.environ.get("FYERS_PIN", "")
FYERS_TOTP_KEY = os.environ.get("FYERS_TOTP_KEY", "")
FYERS_APP_ID = os.environ.get("FYERS_APP_ID", "")
FYERS_SECRET_ID = os.environ.get("FYERS_SECRET_ID", "")
FYERS_REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "http://127.0.0.1:8080")


def _totp(key, time_step=30, digits=6, digest="sha1"):
    key = base64.b32decode(key.upper() + "=" * ((8 - len(key)) % 8))
    counter = struct.pack(">Q", int(time.time() / time_step))
    mac = hmac.new(key, counter, digest).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">L", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary)[-digits:].zfill(digits)


def get_fyers_access_token():
    """Fully automated Fyers login: TOTP + PIN -> auth_code -> access_token.

    Uses Fyers' internal login endpoints (community-documented, not part
    of Fyers' official public API docs), so this could break if Fyers
    changes their web login flow.
    """
    missing = [name for name, val in [
        ("FYERS_CLIENT_ID", FYERS_CLIENT_ID), ("FYERS_PIN", FYERS_PIN),
        ("FYERS_TOTP_KEY", FYERS_TOTP_KEY), ("FYERS_APP_ID", FYERS_APP_ID),
        ("FYERS_SECRET_ID", FYERS_SECRET_ID),
    ] if not val]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    s = requests.Session()
    s.headers.update(headers)

    print("[AUTH] Step 1/5: requesting login OTP flow...")
    data1 = json.dumps({
        "fy_id": base64.b64encode(FYERS_CLIENT_ID.encode()).decode(),
        "app_id": "2",
    })
    r1 = s.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2", data=data1, timeout=15)
    print(f"[AUTH] Step 1 status={r1.status_code} body={r1.text[:200]!r}")
    r1.raise_for_status()
    request_key = r1.json()["request_key"]

    print("[AUTH] Step 2/5: verifying TOTP...")
    data2 = json.dumps({"request_key": request_key, "otp": _totp(FYERS_TOTP_KEY)})
    r2 = s.post("https://api-t2.fyers.in/vagator/v2/verify_otp", data=data2, timeout=15)
    print(f"[AUTH] Step 2 status={r2.status_code} body={r2.text[:200]!r}")
    if r2.status_code != 200:
        raise RuntimeError(f"Fyers TOTP verification failed: {r2.text[:300]}")
    request_key = r2.json()["request_key"]

    print("[AUTH] Step 3/5: verifying PIN...")
    data3 = json.dumps({
        "request_key": request_key,
        "identity_type": "pin",
        "identifier": base64.b64encode(str(FYERS_PIN).encode()).decode(),
    })
    r3 = s.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2", data=data3, timeout=15)
    print(f"[AUTH] Step 3 status={r3.status_code} body={r3.text[:200]!r}")
    if r3.status_code != 200:
        raise RuntimeError(f"Fyers PIN verification failed: {r3.text[:300]}")
    temp_token = r3.json()["data"]["access_token"]

    print("[AUTH] Step 4/5: exchanging for app-scoped auth_code...")
    headers2 = {"authorization": f"Bearer {temp_token}", "content-type": "application/json; charset=UTF-8"}
    data4 = json.dumps({
        "fyers_id": FYERS_CLIENT_ID,
        "app_id": FYERS_APP_ID.split("-")[0],
        "redirect_uri": FYERS_REDIRECT_URI,
        "appType": "100",
        "code_challenge": "",
        "state": "state",
        "scope": "",
        "nonce": "",
        "response_type": "code",
        "create_cookie": True,
    })
    r4 = s.post("https://api.fyers.in/api/v2/token", headers=headers2, data=data4, timeout=15)
    print(f"[AUTH] Step 4 status={r4.status_code} body={r4.text[:300]!r}")
    if r4.status_code != 308:
        raise RuntimeError(f"Fyers auth_code exchange failed: status={r4.status_code} body={r4.text[:300]}")
    parsed = urlparse(r4.json()["Url"])
    auth_code = parse_qs(parsed.query)["auth_code"][0]

    print("[AUTH] Step 5/5: exchanging auth_code for final access_token...")
    session = fyersModel.SessionModel(
        client_id=FYERS_APP_ID,
        secret_key=FYERS_SECRET_ID,
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    response = session.generate_token()
    if "access_token" not in response:
        raise RuntimeError(f"Fyers token generation failed: {response}")
    print("[AUTH] Success — access_token obtained.")
    return response["access_token"]


def main():
    print("=" * 55)
    print("  Fyers API Diagnostic — Stage 1")
    print("=" * 55)

    token = get_fyers_access_token()

    fyers = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=token, is_async=False, log_path="")

    print("\n[DIAG] Fetching profile to confirm token works...")
    profile = fyers.get_profile()
    print(f"[DIAG] Profile response: {json.dumps(profile, indent=2)[:500]}")

    print("\n[DIAG] Fetching option chain for NSE:NIFTY50-INDEX...")
    data = {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 3, "timestamp": ""}
    resp = fyers.optionchain(data=data)

    print("\n[DIAG] ===== FULL RAW RESPONSE BELOW =====")
    print(json.dumps(resp, indent=2))
    print("[DIAG] ===== END RAW RESPONSE =====")


if __name__ == "__main__":
    main()
