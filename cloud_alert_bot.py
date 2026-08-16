"""
NSE Signal Alert Bot  v3.3  (Fyers API edition — Nifty-trend gated)
=================================================
Runs as a single scan per invocation, triggered on a schedule by
GitHub Actions (.github/workflows/scan.yml) — no server, no PC needed.

v3.3 change log (from v3.2) — added a NIFTY momentum gate after
cross-checking several days of signal_log.csv against Nifty's own
5-min chart at signal time:
  - On days where Nifty was flat/falling, SELL signals had a solid
    hit rate. On the one day Nifty rallied sharply mid-session, every
    single SELL signal that day failed (price reversed against the
    signal). The individual stock's own option-chain setup wasn't
    the problem — the broad index move overran it.
  - Fix: track NIFTY's own spot price across scans (persisted in
    state.json) and compute a short-term trend (UP / DOWN / FLAT)
    each run. A BUY is suppressed if Nifty is trending DOWN; a SELL
    is suppressed if Nifty is trending UP. Signals that pass their
    own PCR/score bar but get blocked by this gate are still logged
    to signal_log.csv (as "BUY-SKIPPED-NIFTY" / "SELL-SKIPPED-NIFTY")
    so you can verify the filter's behavior directly in GitHub —
    but they do NOT send a Telegram alert, since they're the ones
    we're trying to filter out.
  - NIFTY_TREND_THRESHOLD_PCT and NIFTY_TREND_LOOKBACK_MIN are best
    first-guess values (0.15% over 30 min) — tune these once you
    have a few weeks of BUY-SKIPPED / SELL-SKIPPED rows to compare
    against what actually happened to those symbols afterward.

v3.2 change log (from v3.1) — thresholds recalibrated against a 12-day
historical backtest of signal_log.csv to target ~5 signals/day per side:
  - BUY: score bar raised to >= 80 (was 70) — this requires a "pure"
    signal (no conflicting bearish flags), not just a loose score pass.
    PCR bar unchanged at >= 1.15.
  - SELL: PCR bar tightened to <= 0.45 (was 0.70), score bar tightened
    to <= 15 (was 25, also now a "pure" signal requirement).

v3.1 change log (from v3.0):
  - Removed BUY-CORE / SELL-CORE (unfiltered "no PCR/score filter" alerts)
    and COIL (pre-breakout prep alert) — these were flooding Telegram with
    non-actionable noise. Only BUY and SELL remain.
  - hasCESC / hasPESB / hasCESB / hasPESC now require >=2 strikes agreeing
    (was: any single strike), to filter out single-strike option-chain noise.
  - BUY threshold tightened: PCR >= 1.15 (was 1.0), score >= 70 (was 62).
  - SELL threshold tightened: PCR <= 0.70 (was 0.85), score <= 25 (was 38).

v3.0 replaced v2.x's NSE-website scraping (which was permanently blocked
by NSE's Akamai bot protection — every request from GitHub's IPs was
rejected) with the official Fyers broker API. Fyers access tokens expire
every 24h, so each run performs a fully-automated login (TOTP + PIN, no
browser) once, then reuses that token for all symbol fetches.

Each run:
  1. Logs into Fyers automatically (TOTP + PIN -> auth_code -> access_token).
  2. Loads prior signal state from state.json (so we don't re-alert
     on a signal that's still active from the last run).
  3. Scans all F&O stocks once via Fyers' option-chain API.
  4. Sends Telegram alerts for newly-fired BUY/SELL signals.
  5. Saves updated state back to state.json (the workflow commits
     this file back to the repo).

OI classification: Short Cover / Short Build are distinguished using BOTH
open-interest direction (oich) AND the option's premium price direction
(ltpch) — the standard 2x2 matrix, not OI direction alone. This matches
how platforms like Sensibull/Opstra label OI changes.

Signals:
  BUY   alert: CE Short Cover (>=2 strikes) + PE Short Build (>=2 strikes)
               + PCR >= 1.15 + Score >= 80 (pure signal, no conflicting flags)
  SELL  alert: CE Short Build (>=2 strikes) + PE Short Cover (>=2 strikes)
               + PCR <= 0.45 + Score <= 15 (pure signal, no conflicting flags)

Every fired signal is also appended to signal_log.csv (committed back to the
repo alongside state.json) so you can tally how often each type fires.

Required environment variables (set as GitHub Actions secrets):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  FYERS_CLIENT_ID, FYERS_PIN, FYERS_TOTP_KEY, FYERS_APP_ID,
  FYERS_SECRET_ID, FYERS_REDIRECT_URI
"""

import os
import csv
import time
import json
import base64
import hmac
import struct
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel

# GitHub Actions runners run in UTC — all market-hours logic must be
# anchored to IST explicitly, never to naive datetime.now().
IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)

# ── CONFIG (set as GitHub Actions secrets) ─────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

FYERS_CLIENT_ID    = os.environ.get("FYERS_CLIENT_ID", "")
FYERS_PIN          = os.environ.get("FYERS_PIN", "")
FYERS_TOTP_KEY     = os.environ.get("FYERS_TOTP_KEY", "")
FYERS_APP_ID       = os.environ.get("FYERS_APP_ID", "")
FYERS_SECRET_ID    = os.environ.get("FYERS_SECRET_ID", "")
FYERS_REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "http://127.0.0.1:8080")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LOG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log.csv")

# ── Nifty trend gate settings (v3.3) ──────────────────────────────────────
# A BUY is suppressed while Nifty is trending DOWN; a SELL is suppressed
# while Nifty is trending UP. Tune these once you have a few weeks of
# BUY-SKIPPED-NIFTY / SELL-SKIPPED-NIFTY rows in signal_log.csv to check
# against what those symbols actually did afterward.
NIFTY_TREND_LOOKBACK_MIN = 30      # how far back to compare Nifty's spot
NIFTY_TREND_THRESHOLD_PCT = 0.15   # % move over that window to call it "trending"

# ── F&O Symbols to monitor ────────────────────────────────────────────────────
FO_STOCKS = [
    "NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50","BSE","RELIANCE","SBIN","ADANIENT","HDFCBANK",
    "ADANIGREEN","MCX","ICICIBANK","BAJFINANCE","TCS","INFY","AXISBANK","ADANIENSOL","WIPRO","TATASTEEL",
    "ADANIPOWER","BHARTIARTL","BHEL","VEDL","CANBK","BANKBARODA","HINDZINC","TITAN","NATIONALUM","SHRIRAMFIN",
    "M&M","LT","HINDALCO","PFC","ULTRACEMCO","COALINDIA","BEL","ITC","SUNPHARMA","ANGELONE",
    "DIXON","CGPOWER","ASIANPAINT","NHPC","MARUTI","SAIL","HINDUNILVR","KOTAKBANK","TMPV","NMDC",
    "RECLTD","TRENT","NTPC","CHOLAFIN","COFORGE","INDIGO","YESBANK","AMBER","TECHM","ASHOKLEY",
    "HEROMOTOCO","AMBUJACEM","NBCC","EICHERMOT","ADANIPORTS","INDIANB","FEDERALBNK","TVSMOTOR","HDFCAMC","RVNL",
    "360ONE","PNB","BAJAJ-AUTO","POLYCAB","BAJAJFINSV","KAYNES","LUPIN","HCLTECH","HDFCLIFE","PATANJALI",
    "CUMMINSIND","LAURUSLABS","UNIONBANK","GRASIM","PERSISTENT","LTF","HAL","INDUSTOWER","JSWSTEEL","IDFCFIRSTB",
    "VMM","ONGC","POWERGRID","MOTHERSON","OFSS","DLF","AUBANK","AUROPHARMA","TORNTPHARM","NESTLEIND",
    "VOLTAS","APLAPOLLO","BANKINDIA","ABB","MUTHOOTFIN","INDUSINDBK","VBL","LICHSGFIN","UPL","BPCL",
    "COCHINSHIP","APOLLOHOSP","DMART","FORTIS","IOC","CIPLA","MARICO","NAUKRI","MAZDOCK","RBLBANK",
    "CDSL","ABCAPITAL","GAIL","DIVISLAB","ICICIGI","MAXHEALTH","SBILIFE","LICI","CROMPTON","SIEMENS",
    "BANDHANBNK","OIL","LODHA","JINDALSTEL","PRESTIGE","HINDPETRO","UNOMINDA","EXIDEIND","TATACONSUM","GLENMARK",
    "GODREJPROP","KEI","KFINTECH","TATAPOWER","BIOCON","PNBHOUSING","LTM","ZYDUSLIFE","BOSCHLTD","DRREDDY",
    "SONACOMS","PGEL","JSWENERGY","HAVELLS","NAM-INDIA","CONCOR","PHOENIXLTD","BRITANNIA","MPHASIS","ICICIPRULI",
    "DABUR","PETRONET","IRFC","CAMS","BLUESTARCO","INDHOTEL","ALKEM","BHARATFORG","MANAPPURAM","TATAELXSI",
    "PIDILITIND","BAJAJHLDNG","PAGEIND","RADICO","IEX","KPITTECH","GODREJCP","IREDA","ASTRAL","TIINDIA",
    "GODFRYPHLP","JUBLFOOD","SHREECEM","NUVAMA","MOTILALOFS","SUPREMEIND","DALBHARAT","SRF","OBEROIRLTY","MANKIND",
    "COLPAL","UNITDSPR","PIIND",
]

# ── Fyers symbol construction ──────────────────────────────────────────────
INDEX_SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",
}

def fyers_symbol(sym):
    if sym in INDEX_SYMBOLS:
        return INDEX_SYMBOLS[sym]
    return f"NSE:{sym}-EQ"

# ── State persistence (survives across GitHub Actions runs) ──────────────────
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[STATE] Save error: {e}")

# ── Nifty trend tracking (v3.3, persisted across runs via state.json) ────
def update_nifty_trend(state, nifty_spot):
    """
    Records NIFTY's spot price with a timestamp under state["_nifty_history"],
    and returns a trend label: 'UP', 'DOWN', or 'FLAT', based on NIFTY's own
    move over the last NIFTY_TREND_LOOKBACK_MIN minutes.
    """
    now_ts = now_ist().timestamp()
    history = state.setdefault("_nifty_history", [])

    # Prune anything older than the lookback window
    cutoff = now_ts - NIFTY_TREND_LOOKBACK_MIN * 60
    history[:] = [h for h in history if h["ts"] >= cutoff]

    trend = "FLAT"
    if history:
        oldest_spot = history[0]["spot"]
        if oldest_spot:
            pct_move = (nifty_spot - oldest_spot) / oldest_spot * 100
            if pct_move >= NIFTY_TREND_THRESHOLD_PCT:
                trend = "UP"
            elif pct_move <= -NIFTY_TREND_THRESHOLD_PCT:
                trend = "DOWN"

    history.append({"ts": now_ts, "spot": nifty_spot})
    return trend

# ── Signal frequency log (CSV, newest signal always inserted right after
# the header row, so today's signals show at the TOP instead of the bottom) ──
HEADER_ROW = ["timestamp_ist", "signal", "symbol", "cmp", "pcr", "score", "maxPain", "mpGap"]

def log_signal(signal_type, d):
    try:
        new_row = [
            now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            signal_type, d["sym"], d["cmp"], d["pcr"], d["score"], d["maxPain"], d["mpGap"],
        ]

        existing_rows = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", newline="") as f:
                rows = list(csv.reader(f))
                if rows:
                    # rows[0] is the old header; every row after it is
                    # already newest-first from prior runs, so keep as-is.
                    existing_rows = rows[1:]

        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(HEADER_ROW)
            writer.writerow(new_row)
            writer.writerows(existing_rows)
    except Exception as e:
        print(f"[LOG] Error: {e}")

# ── OI daily tracking log (v3.4, NEW) ─────────────────────────────────────
# Tracks, per symbol, a rolling 2-day snapshot of combined option-chain OI
# (CE total + PE total, from analyse()) and spot price, persisted across
# runs under state["_oi_daily"][sym]. Since GitHub Actions runs this
# script repeatedly during market hours, each symbol's "d1" slot is
# refreshed with the LATEST value on every scan of the current day (so by
# the last scan before close, it holds end-of-day figures, matching how
# NSE's own "Change in Open Interest" report and the Pine dashboard's OI
# tracker both compare day-over-day CLOSE values, not intraday ticks).
#
# The first scan of a NEW day is what actually produces a log row: at
# that point we know (a) the OI/price change on the prior day (comparing
# its own d1 vs the d2 snapshot from the day before that), AND (b) we can
# immediately compute the "outcome" — today's opening/current price vs
# that prior day's close — since today's price is already in hand. So
# every row written to oi_tracking_log.csv is complete on arrival; there
# is no separate "come back and fill in the outcome later" step.
#
# NOTE ON DATA SOURCE: this uses combined OPTIONS-CHAIN OI (CE+PE totals,
# already computed in analyse() for the PCR calculation — zero extra
# Fyers API calls), not the underlying's FUTURES open interest specifically.
# It's a reasonable proxy and uses the same PCR data your BUY/SELL signals
# already rely on, but it is a different number than a single futures-
# contract OI figure (e.g. what the "NSE:<SYM>1!_OI" ticker gives you on
# the TradingView side). If you want true futures OI here too, that would
# need one additional Fyers API call per symbol per scan.
OI_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oi_tracking_log.csv")
OI_HEADER_ROW = ["date", "symbol", "oi_pct_change", "price_pct_change", "classification", "next_day_price_change_pct"]

def classify_oi(price_pct, oi_pct):
    """Standard 2x2 price+OI classification. Returns 'N/A' if either
    input is missing (e.g. a symbol's OI or price was 0/unavailable that
    day, so a % change couldn't be computed)."""
    if price_pct is None or oi_pct is None:
        return "N/A"
    if price_pct > 0 and oi_pct > 0:
        return "Long Buildup"
    if price_pct < 0 and oi_pct > 0:
        return "Short Buildup"
    if price_pct > 0 and oi_pct < 0:
        return "Short Covering"
    if price_pct < 0 and oi_pct < 0:
        return "Long Unwinding"
    return "Flat"

def log_oi_row(date_str, sym, oi_pct, price_pct, classification, outcome_pct):
    try:
        new_row = [
            date_str, sym,
            "" if oi_pct is None else round(oi_pct, 3),
            "" if price_pct is None else round(price_pct, 3),
            classification,
            "" if outcome_pct is None else round(outcome_pct, 3),
        ]

        existing_rows = []
        if os.path.exists(OI_LOG_FILE):
            with open(OI_LOG_FILE, "r", newline="") as f:
                rows = list(csv.reader(f))
                if rows:
                    existing_rows = rows[1:]

        with open(OI_LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(OI_HEADER_ROW)
            writer.writerow(new_row)
            writer.writerows(existing_rows)
    except Exception as e:
        print(f"[OI-LOG] Error: {e}")

def track_oi_history(state, sym, combined_oi, spot):
    """Updates state["_oi_daily"][sym]'s rolling 2-day snapshot, and — on
    the first scan of a new trading day for this symbol — logs the
    completed prior-day row (OI% change, price% change, classification,
    and the outcome that followed it) to oi_tracking_log.csv."""
    if not combined_oi or not spot:
        return

    today_str = now_ist().strftime("%Y-%m-%d")
    daily = state.setdefault("_oi_daily", {})
    entry = daily.get(sym)

    if entry is None:
        # First time ever seeing this symbol — seed d1, nothing to log yet.
        daily[sym] = {"d1_date": today_str, "d1_oi": combined_oi, "d1_price": spot}
        return

    if entry.get("d1_date") == today_str:
        # Same trading day — just refresh d1 with the latest values so it
        # approaches the end-of-day figure by the last scan before close.
        entry["d1_oi"] = combined_oi
        entry["d1_price"] = spot
        return

    # New day detected — entry currently holds what was "today" as of the
    # last run, i.e. it's now the completed PRIOR day.
    if "d2_date" in entry and entry.get("d2_oi") and entry.get("d2_price"):
        d1_oi, d1_price = entry["d1_oi"], entry["d1_price"]
        d2_oi, d2_price = entry["d2_oi"], entry["d2_price"]
        oi_pct    = (d1_oi - d2_oi) / d2_oi * 100 if d2_oi else None
        price_pct = (d1_price - d2_price) / d2_price * 100 if d2_price else None
        outcome_pct = (spot - d1_price) / d1_price * 100 if d1_price else None
        classification = classify_oi(price_pct, oi_pct)
        log_oi_row(entry["d1_date"], sym, oi_pct, price_pct, classification, outcome_pct)

    # Shift the rolling window: today's incoming snapshot becomes the new
    # d1; the old d1 becomes d2.
    daily[sym] = {
        "d2_date": entry.get("d1_date"), "d2_oi": entry.get("d1_oi"), "d2_price": entry.get("d1_price"),
        "d1_date": today_str, "d1_oi": combined_oi, "d1_price": spot,
    }

# ── Fyers automated login (TOTP + PIN, no browser) ────────────────────────
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

    data1 = json.dumps({
        "fy_id": base64.b64encode(FYERS_CLIENT_ID.encode()).decode(),
        "app_id": "2",
    })
    r1 = s.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2", data=data1, timeout=15)
    r1.raise_for_status()
    request_key = r1.json()["request_key"]

    data2 = json.dumps({"request_key": request_key, "otp": _totp(FYERS_TOTP_KEY)})
    r2 = s.post("https://api-t2.fyers.in/vagator/v2/verify_otp", data=data2, timeout=15)
    if r2.status_code != 200:
        raise RuntimeError(f"Fyers TOTP verification failed: {r2.text[:300]}")
    request_key = r2.json()["request_key"]

    data3 = json.dumps({
        "request_key": request_key,
        "identity_type": "pin",
        "identifier": base64.b64encode(str(FYERS_PIN).encode()).decode(),
    })
    r3 = s.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2", data=data3, timeout=15)
    if r3.status_code != 200:
        raise RuntimeError(f"Fyers PIN verification failed: {r3.text[:300]}")
    temp_token = r3.json()["data"]["access_token"]

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
    if r4.status_code != 308:
        raise RuntimeError(f"Fyers auth_code exchange failed: status={r4.status_code} body={r4.text[:300]}")
    parsed = urlparse(r4.json()["Url"])
    auth_code = parse_qs(parsed.query)["auth_code"][0]

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
    return response["access_token"]

# ── Fetch option chain for one symbol via Fyers ───────────────────────────
_diag_fetch_dumped = False

def fetch_option_chain(fyers, sym):
    global _diag_fetch_dumped
    try:
        data = {"symbol": fyers_symbol(sym), "strikecount": 5, "timestamp": ""}
        resp = fyers.optionchain(data=data)
        if resp.get("s") == "ok":
            return resp
        if not _diag_fetch_dumped:
            _diag_fetch_dumped = True
            print(f"[DIAG] Fetch failed for {sym} ({fyers_symbol(sym)}): {resp}")
        return None
    except Exception as e:
        if not _diag_fetch_dumped:
            _diag_fetch_dumped = True
            print(f"[DIAG] Fetch exception for {sym}: {type(e).__name__}: {e}")
        return None

# ── Analyse option chain ──────────────────────────────────────────────────────
def analyse(sym, resp):
    try:
        chain = resp.get("data", {}).get("optionsChain", [])
        if not chain:
            return None

        # First entry is always the underlying itself (option_type=="", strike_price==-1)
        underlying = chain[0]
        spot = float(underlying.get("ltp", 0) or 0)
        rows = chain[1:]
        if not spot or not rows:
            return None

        # PCR across the fetched near-ATM window
        ce_oi_total = sum(r.get("oi", 0) or 0 for r in rows if r.get("option_type") == "CE")
        pe_oi_total = sum(r.get("oi", 0) or 0 for r in rows if r.get("option_type") == "PE")
        pcr = round(pe_oi_total / ce_oi_total, 2) if ce_oi_total else 0

        ce_sc, pe_sb, ce_sb, pe_sc = [], [], [], []
        pe_wall = 0

        for r in rows:
            sp = r.get("strike_price", 0)
            opt = r.get("option_type")
            oi = r.get("oi", 0) or 0
            oich = r.get("oich", 0) or 0
            ltpch = r.get("ltpch", 0) or 0

            # Classification requires BOTH the OI direction AND the premium
            # price direction (the standard 2x2 OI-interpretation matrix).
            # OI falling alone is ambiguous: it's Short Cover only if premium
            # rose too; if premium fell instead, that's Long Unwind, a
            # different (non-bullish) signal that must NOT count as Short
            # Cover. Same logic mirrored for PE.
            if opt == "CE":
                if oi > 0 and oich < 0 and ltpch > 0: ce_sc.append(sp)   # CE Short Cover
                if oi > 0 and oich > 0 and ltpch < 0: ce_sb.append(sp)   # CE Short Build
            elif opt == "PE":
                if oi > 0 and oich > 0 and ltpch < 0:
                    pe_sb.append(sp)                                      # PE Short Build
                    pe_wall += 1
                if oi > 0 and oich < 0 and ltpch > 0: pe_sc.append(sp)   # PE Short Cover

        # Score (simplified)
        bull = 0; bear = 0
        if pcr >= 1.3: bull += 3
        elif pcr >= 1.0: bull += 1.5
        elif pcr <= 0.7: bear += 3
        elif pcr < 1.0: bear += 1.5
        if ce_sc: bull += 2
        if pe_sb: bull += 2
        if ce_sb: bear += 2
        if pe_sc: bear += 2
        total = bull + bear or 1
        score = round(bull / total * 100)

        # MaxPain
        ce_map = {r.get("strike_price", 0): r.get("oi", 0) or 0 for r in rows if r.get("option_type") == "CE"}
        pe_map = {r.get("strike_price", 0): r.get("oi", 0) or 0 for r in rows if r.get("option_type") == "PE"}
        strikes = sorted(set(list(ce_map.keys()) + list(pe_map.keys())))
        max_pain = 0
        min_pain = float("inf")
        for t in strikes:
            pain = sum(max(0, (k - t)) * ce_map.get(k, 0) + max(0, (t - k)) * pe_map.get(k, 0) for k in strikes)
            if pain < min_pain:
                min_pain = pain; max_pain = t

        mp_gap = round((spot - max_pain) / max_pain * 100, 2) if max_pain else 0

        # Get S1/R1
        top_ce = sorted(ce_map.items(), key=lambda x: -x[1])
        top_pe = sorted(pe_map.items(), key=lambda x: -x[1])
        r1 = top_ce[0][0] if top_ce else 0
        s1 = top_pe[0][0] if top_pe else 0

        return {
            "sym": sym, "cmp": spot, "pcr": pcr, "score": score,
            "maxPain": max_pain, "mpGap": mp_gap,
            "r1": r1, "s1": s1, "peWall": pe_wall,
            # v3.1: require >=2 strikes agreeing, not just any single strike,
            # to filter out single-strike option-chain noise.
            "hasCESC": len(ce_sc) >= 2, "hasPESB": len(pe_sb) >= 2,
            "hasCESB": len(ce_sb) >= 2, "hasPESC": len(pe_sc) >= 2,
            "ceSBCount": len(ce_sb), "peSBCount": len(pe_sb),
            # v3.4: combined CE+PE total OI, exposed so track_oi_history()
            # can log day-over-day OI% change without any extra API calls.
            "ceOiTotal": ce_oi_total, "peOiTotal": pe_oi_total,
            "combinedOi": ce_oi_total + pe_oi_total,
        }
    except Exception:
        return None

# ── Telegram send ─────────────────────────────────────────────────────────────
def send(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"[TG] Error: {e}")
        return False

# ── Check and alert (v3.3: BUY / SELL, gated by Nifty trend) ─────────────────
def check_and_alert(d, active_signals, nifty_trend):
    sym = d["sym"]
    now = now_ist().strftime("%H:%M")

    # ── BUY: CE-SC + PE-SB (>=2 strikes each) + PCR >= 1.15 + Score >= 80 (pure) ──
    if d["hasCESC"] and d["hasPESB"] and d["pcr"] >= 1.15 and d["score"] >= 80:
        key = f"BULL_{sym}"
        if active_signals.get(key) != "BULL":
            active_signals[key] = "BULL"

            if nifty_trend == "DOWN":
                # Passed its own criteria, but Nifty is fighting it — log it
                # for visibility (so you can verify the filter's calls) but
                # don't send a Telegram alert for it.
                print(f"[SKIP] BUY {sym} @ {d['cmp']} — Nifty trending DOWN")
                log_signal("BUY-SKIPPED-NIFTY", d)
            else:
                msg = (
                    f"⚡ <b>BUY SIGNAL — {sym}</b>\n\n"
                    f"🕐 {now}  |  💰 CMP: ₹{d['cmp']}\n"
                    f"📊 PCR: {d['pcr']}  |  Score: {d['score']}/100\n"
                    f"🎯 MaxPain: ₹{d['maxPain']}  ({d['mpGap']:+.1f}%)\n"
                    f"🛡️ S1: ₹{d['s1']}  |  🎯 R1: ₹{d['r1']}\n"
                    f"🧱 PeWall: {d['peWall']} strikes\n"
                    f"📈 Nifty trend: {nifty_trend}\n\n"
                    f"✅ <b>CE Short Cover + PE Short Build fired</b>\n"
                    f"<i>Institutional floor confirmed</i>"
                )
                send(msg)
                print(f"[ALERT] BUY — {sym} @ {d['cmp']}")
                log_signal("BUY", d)
    else:
        active_signals.pop(f"BULL_{sym}", None)

    # ── SELL: CE-SB + PE-SC (>=2 strikes each) + PCR <= 0.45 + Score <= 15 (pure) ─
    if d["hasCESB"] and d["hasPESC"] and d["pcr"] <= 0.45 and d["score"] <= 15:
        key = f"BEAR_{sym}"
        if active_signals.get(key) != "BEAR":
            active_signals[key] = "BEAR"

            if nifty_trend == "UP":
                # Passed its own criteria, but Nifty is fighting it — log it
                # for visibility (so you can verify the filter's calls) but
                # don't send a Telegram alert for it.
                print(f"[SKIP] SELL {sym} @ {d['cmp']} — Nifty trending UP")
                log_signal("SELL-SKIPPED-NIFTY", d)
            else:
                msg = (
                    f"🔻 <b>SELL SIGNAL — {sym}</b>\n\n"
                    f"🕐 {now}  |  💰 CMP: ₹{d['cmp']}\n"
                    f"📊 PCR: {d['pcr']}  |  Score: {d['score']}/100\n"
                    f"🎯 MaxPain: ₹{d['maxPain']}  ({d['mpGap']:+.1f}%)\n"
                    f"🛡️ R1: ₹{d['r1']}  |  🎯 S1: ₹{d['s1']}\n"
                    f"🧱 CeWall: {d['ceSBCount']} strikes\n"
                    f"📉 Nifty trend: {nifty_trend}\n\n"
                    f"✅ <b>CE Short Build + PE Short Cover fired</b>\n"
                    f"<i>Institutional ceiling confirmed</i>"
                )
                send(msg)
                print(f"[ALERT] SELL — {sym} @ {d['cmp']}")
                log_signal("SELL", d)
    else:
        active_signals.pop(f"BEAR_{sym}", None)

# ── Market hours check ────────────────────────────────────────────────────────
def is_market_open():
    now = now_ist()
    h, m = now.hour, now.minute
    # IST 9:15 AM to 3:35 PM, Mon-Fri
    if now.weekday() >= 5: return False  # weekend
    if h < 9 or (h == 9 and m < 15): return False
    if h > 15 or (h == 15 and m > 35): return False
    return True

# ── Single scan (one run of this script = one scan) ──────────────────────────
def run():
    print("=" * 55)
    print("  NSE Signal Alert Bot  v3.4 (Fyers API edition — Nifty-trend gated + OI tracking)")
    print("=" * 55)
    print(f"  Symbols: {len(FO_STOCKS)}")
    print(f"  Telegram: {'configured' if TELEGRAM_TOKEN != 'YOUR_BOT_TOKEN' else 'NOT SET'}")
    print("=" * 55)

    if not is_market_open():
        print(f"[{now_ist().strftime('%H:%M')}] Market closed. Skipping scan.")
        return

    active_signals = load_state()

    print(f"[{now_ist().strftime('%H:%M:%S')}] Logging into Fyers...")
    try:
        token = get_fyers_access_token()
    except Exception as e:
        print(f"[FYERS] Login failed: {e}")
        return
    fyers = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=token, is_async=False, log_path="")
    print("[FYERS] Login OK.")

    errors = 0
    diag = {
        "fetched_ok": 0, "fetched_empty": 0,
        "analysed_ok": 0, "analysed_none": 0,
        "any_ce_sc": 0, "any_pe_sb": 0, "any_ce_sb": 0, "any_pe_sc": 0,
    }

    # ── Compute Nifty's own trend FIRST, before scanning the rest (v3.3) ──
    # This determines whether BUY/SELL signals on individual stocks get
    # gated later in the loop.
    nifty_trend = "FLAT"
    try:
        nifty_data = fetch_option_chain(fyers, "NIFTY")
        if nifty_data:
            nifty_result = analyse("NIFTY", nifty_data)
            if nifty_result:
                nifty_trend = update_nifty_trend(active_signals, nifty_result["cmp"])
    except Exception as e:
        print(f"[INDEX] Nifty trend check failed, defaulting to FLAT: {e}")
    print(f"[INDEX] Nifty trend this scan: {nifty_trend}")

    print(f"[{now_ist().strftime('%H:%M:%S')}] Scanning {len(FO_STOCKS)} symbols...")

    for sym in FO_STOCKS:
        try:
            data = fetch_option_chain(fyers, sym)
            if data:
                diag["fetched_ok"] += 1
                result = analyse(sym, data)
                if result:
                    diag["analysed_ok"] += 1
                    if result["hasCESC"]: diag["any_ce_sc"] += 1
                    if result["hasPESB"]: diag["any_pe_sb"] += 1
                    if result["hasCESB"]: diag["any_ce_sb"] += 1
                    if result["hasPESC"]: diag["any_pe_sc"] += 1
                    check_and_alert(result, active_signals, nifty_trend)
                    track_oi_history(active_signals, sym, result["combinedOi"], result["cmp"])
                else:
                    diag["analysed_none"] += 1
            else:
                diag["fetched_empty"] += 1
            time.sleep(0.2)  # stay comfortably under Fyers' 10 req/sec limit
        except Exception:
            errors += 1

    print(f"  Done. {len(FO_STOCKS)} symbols. Errors: {errors}.")
    print(f"[DIAG] Fetch OK: {diag['fetched_ok']}  Fetch empty/failed: {diag['fetched_empty']}")
    print(f"[DIAG] Analysed OK: {diag['analysed_ok']}  Analysed None (no spot/rows): {diag['analysed_none']}")
    print(f"[DIAG] Symbols with any CE-Short-Cover: {diag['any_ce_sc']}  "
          f"any PE-Short-Build: {diag['any_pe_sb']}  "
          f"any CE-Short-Build: {diag['any_ce_sb']}  "
          f"any PE-Short-Cover: {diag['any_pe_sc']}")

    save_state(active_signals)

if __name__ == "__main__":
    run()
