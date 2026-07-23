"""
NSE Signal Alert Bot  v3.0  (Fyers API edition)
=================================================
Runs as a single scan per invocation, triggered on a schedule by
GitHub Actions (.github/workflows/scan.yml) — no server, no PC needed.

v3.0 replaces v2.x's NSE-website scraping (which was permanently blocked
by NSE's Akamai bot protection — every request from GitHub's IPs was
rejected) with the official Fyers broker API. Fyers access tokens expire
every 24h, so each run performs a fully-automated login (TOTP + PIN, no
browser) once, then reuses that token for all symbol fetches.

Each run:
  1. Logs into Fyers automatically (TOTP + PIN -> auth_code -> access_token).
  2. Loads prior signal state from state.json (so we don't re-alert
     on a signal that's still active from the last run).
  3. Scans all F&O stocks once via Fyers' option-chain API.
  4. Sends Telegram alerts for newly-fired signals.
  5. Saves updated state back to state.json (the workflow commits
     this file back to the repo).

OI classification: Short Cover / Short Build are distinguished using BOTH
open-interest direction (oich) AND the option's premium price direction
(ltpch) — the standard 2x2 matrix, not OI direction alone. This matches
how platforms like Sensibull/Opstra label OI changes.

Signals:
  BUY       alert: CE Short Cover + PE Short Build both firing, plus PCR/score filter
  SELL      alert: CE Short Build + PE Short Cover both firing, plus PCR/score filter
  BUY-CORE  alert: CE Short Cover + PE Short Build both firing — no other filter
  SELL-CORE alert: CE Short Build + PE Short Cover both firing — no other filter
  Coil      alert: Coiled Spring pre-breakout/breakdown detected

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

# ── Signal frequency log (CSV, appended every fired alert) ───────────────────
def log_signal(signal_type, d):
    try:
        is_new = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["timestamp_ist", "signal", "symbol", "cmp", "pcr", "score", "maxPain", "mpGap"])
            writer.writerow([
                now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                signal_type, d["sym"], d["cmp"], d["pcr"], d["score"], d["maxPain"], d["mpGap"],
            ])
    except Exception as e:
        print(f"[LOG] Error: {e}")

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
            "hasCESC": bool(ce_sc), "hasPESB": bool(pe_sb),
            "hasCESB": bool(ce_sb), "hasPESC": bool(pe_sc),
            "ceSBCount": len(ce_sb), "peSBCount": len(pe_sb),
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

# ── Check and alert ───────────────────────────────────────────────────────────
def check_and_alert(d, active_signals):
    sym = d["sym"]
    now = now_ist().strftime("%H:%M")

    # ── BUY: CE-SC + PE-SB + PCR >= 1.0 + Score >= 62 ──────────────────────
    if d["hasCESC"] and d["hasPESB"] and d["pcr"] >= 1.0 and d["score"] >= 62:
        key = f"BULL_{sym}"
        if active_signals.get(key) != "BULL":
            active_signals[key] = "BULL"
            msg = (
                f"⚡ <b>BUY SIGNAL — {sym}</b>\n\n"
                f"🕐 {now}  |  💰 CMP: ₹{d['cmp']}\n"
                f"📊 PCR: {d['pcr']}  |  Score: {d['score']}/100\n"
                f"🎯 MaxPain: ₹{d['maxPain']}  ({d['mpGap']:+.1f}%)\n"
                f"🛡️ S1: ₹{d['s1']}  |  🎯 R1: ₹{d['r1']}\n"
                f"🧱 PeWall: {d['peWall']} strikes\n\n"
                f"✅ <b>CE Short Cover + PE Short Build fired</b>\n"
                f"<i>Institutional floor confirmed</i>"
            )
            send(msg)
            print(f"[ALERT] BUY — {sym} @ {d['cmp']}")
            log_signal("BUY", d)
    else:
        active_signals.pop(f"BULL_{sym}", None)

    # ── BUY-CORE: CE-SC + PE-SB only — no PCR/score filter ──────────────────
    if d["hasCESC"] and d["hasPESB"]:
        key = f"BUYCORE_{sym}"
        if active_signals.get(key) != "BUYCORE":
            active_signals[key] = "BUYCORE"
            msg = (
                f"🟢 <b>BUY-CORE — {sym}</b>\n\n"
                f"🕐 {now}  |  💰 CMP: ₹{d['cmp']}\n"
                f"📊 PCR: {d['pcr']}  |  Score: {d['score']}/100\n\n"
                f"✅ <b>CE Short Cover + PE Short Build fired</b>\n"
                f"<i>Raw OI pattern only — no PCR/score filter</i>"
            )
            send(msg)
            print(f"[ALERT] BUY-CORE — {sym} @ {d['cmp']}")
            log_signal("BUY-CORE", d)
    else:
        active_signals.pop(f"BUYCORE_{sym}", None)

    # ── SELL: CE-SB + PE-SC + PCR <= 0.85 + Score <= 38 ────────────────────
    if d["hasCESB"] and d["hasPESC"] and d["pcr"] <= 0.85 and d["score"] <= 38:
        key = f"BEAR_{sym}"
        if active_signals.get(key) != "BEAR":
            active_signals[key] = "BEAR"
            msg = (
                f"🔻 <b>SELL SIGNAL — {sym}</b>\n\n"
                f"🕐 {now}  |  💰 CMP: ₹{d['cmp']}\n"
                f"📊 PCR: {d['pcr']}  |  Score: {d['score']}/100\n"
                f"🎯 MaxPain: ₹{d['maxPain']}  ({d['mpGap']:+.1f}%)\n"
                f"🛡️ R1: ₹{d['r1']}  |  🎯 S1: ₹{d['s1']}\n"
                f"🧱 CeWall: {d['ceSBCount']} strikes\n\n"
                f"✅ <b>CE Short Build + PE Short Cover fired</b>\n"
                f"<i>Institutional ceiling confirmed</i>"
            )
            send(msg)
            print(f"[ALERT] SELL — {sym} @ {d['cmp']}")
            log_signal("SELL", d)
    else:
        active_signals.pop(f"BEAR_{sym}", None)

    # ── SELL-CORE: CE-SB + PE-SC only — no PCR/score filter ─────────────────
    if d["hasCESB"] and d["hasPESC"]:
        key = f"SELLCORE_{sym}"
        if active_signals.get(key) != "SELLCORE":
            active_signals[key] = "SELLCORE"
            msg = (
                f"🔴 <b>SELL-CORE — {sym}</b>\n\n"
                f"🕐 {now}  |  💰 CMP: ₹{d['cmp']}\n"
                f"📊 PCR: {d['pcr']}  |  Score: {d['score']}/100\n\n"
                f"✅ <b>CE Short Build + PE Short Cover fired</b>\n"
                f"<i>Raw OI pattern only — no PCR/score filter</i>"
            )
            send(msg)
            print(f"[ALERT] SELL-CORE — {sym} @ {d['cmp']}")
            log_signal("SELL-CORE", d)
    else:
        active_signals.pop(f"SELLCORE_{sym}", None)

    # ── COIL BULL: PE-SB×2+ + CE-SB + CMP below MaxPain + PCR >= 1.0 ───────
    if d["peSBCount"] >= 2 and d["hasCESB"] and d["mpGap"] < -0.5 and d["pcr"] >= 1.0:
        key = f"COIL_{sym}"
        if active_signals.get(key) != "COIL":
            active_signals[key] = "COIL"
            msg = (
                f"🔥 <b>COILED SPRING — {sym}</b>\n\n"
                f"🕐 {now}  |  💰 CMP: ₹{d['cmp']}\n"
                f"📊 PCR: {d['pcr']}  |  MaxPain: ₹{d['maxPain']}\n"
                f"📏 MP Gap: {d['mpGap']:+.1f}%  (below = bullish pull)\n"
                f"🧱 PeWall: {d['peWall']} strikes\n\n"
                f"⏳ <b>Pre-Breakout Setup</b>\n"
                f"<i>Wait for CE Short Cover to fire → BUY</i>"
            )
            send(msg)
            print(f"[ALERT] COIL — {sym} @ {d['cmp']}")
            log_signal("COIL", d)
    else:
        active_signals.pop(f"COIL_{sym}", None)

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
    print("  NSE Signal Alert Bot  v3.0 (Fyers API edition)")
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
                    check_and_alert(result, active_signals)
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
