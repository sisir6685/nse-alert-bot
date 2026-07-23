"""
NSE Signal Alert Bot  v2.1  (GitHub Actions edition)
=====================================================
Runs as a single scan per invocation, triggered on a schedule by
GitHub Actions (.github/workflows/scan.yml) — no server, no PC needed,
$0/month.

Each run:
  1. Loads prior signal state from state.json (so we don't re-alert
     on a signal that's still active from the last run).
  2. Refreshes an NSE session (fresh cookies every run since this is
     a brand-new process each time).
  3. Scans all F&O stocks once.
  4. Sends Telegram alerts for newly-fired signals.
  5. Saves updated state back to state.json (the workflow commits
     this file back to the repo).

OI classification (v2.1): Short Cover / Short Build / Long Unwind / Long
Build are distinguished using BOTH open-interest direction AND the option's
premium price direction (the standard 2x2 matrix), not OI direction alone.
This matches how platforms like Sensibull/Opstra label OI changes, and
fixes a v2.0 bug where any OI decrease was assumed to be Short Cover even
when it was actually Long Unwind (and vice versa for Short Build/Long Build).

Signals:
  BUY       alert: CE Short Cover + PE Short Build both firing, plus PCR/score filter
  SELL      alert: CE Short Build + PE Short Cover both firing, plus PCR/score filter
  BUY-CORE  alert: CE Short Cover + PE Short Build both firing — no other filter
  SELL-CORE alert: CE Short Build + PE Short Cover both firing — no other filter
  Coil      alert: Coiled Spring pre-breakout/breakdown detected

Every fired signal is also appended to signal_log.csv (committed back to the
repo alongside state.json) so you can tally how often each type fires.
"""

import os, csv, time, json, requests
from datetime import datetime
from zoneinfo import ZoneInfo

# GitHub Actions runners run in UTC — all market-hours logic must be
# anchored to IST explicitly, never to naive datetime.now().
IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)

# ── CONFIG (set as GitHub Actions secrets) ─────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

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

# ── NSE Headers (required to bypass NSE's bot detection) ─────────────────────
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

session = requests.Session()
session.headers.update(NSE_HEADERS)

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

# ── NSE cookie refresh ────────────────────────────────────────────────────────
def refresh_nse_session():
    try:
        r1 = session.get("https://www.nseindia.com", timeout=10)
        r2 = session.get("https://www.nseindia.com/market-data/live-equity-market", timeout=10)
        print(f"[DIAG] Session warm-up: homepage status={r1.status_code} len={len(r1.content)}  "
              f"live-page status={r2.status_code} len={len(r2.content)}")
        print(f"[DIAG] Cookies set after warm-up: {list(session.cookies.keys())}")
    except Exception as e:
        print(f"[NSE] Session refresh error: {e}")

# ── Fetch option chain for one symbol ────────────────────────────────────────
_diag_fetch_dumped = False

def fetch_option_chain(sym):
    global _diag_fetch_dumped
    try:
        if sym in ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYNXT50"]:
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={sym}"
        else:
            url = f"https://www.nseindia.com/api/option-chain-equities?symbol={sym}"

        r = session.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
        if not _diag_fetch_dumped:
            _diag_fetch_dumped = True
            print(f"[DIAG] Fetch failed for {sym}: status={r.status_code}  "
                  f"content-type={r.headers.get('Content-Type')}  "
                  f"body_snippet={r.text[:300]!r}")
        return None
    except Exception as e:
        if not _diag_fetch_dumped:
            _diag_fetch_dumped = True
            print(f"[DIAG] Fetch exception for {sym}: {type(e).__name__}: {e}")
        return None

# ── Analyse option chain ──────────────────────────────────────────────────────
def analyse(sym, data):
    try:
        rec  = data.get("records", {})
        flt  = data.get("filtered", {})
        rows = flt.get("data") or rec.get("data", [])
        spot = float(rec.get("underlyingValue", 0) or 0)
        if not spot or not rows:
            return None

        # PCR
        ce_oi = flt.get("CE", {}).get("totOI", 0) or 0
        pe_oi = flt.get("PE", {}).get("totOI", 0) or 0
        if not ce_oi:
            for row in rows:
                ce_oi += (row.get("CE") or {}).get("openInterest", 0) or 0
                pe_oi += (row.get("PE") or {}).get("openInterest", 0) or 0
        pcr = round(pe_oi / ce_oi, 2) if ce_oi else 0

        # Find ATM
        atm = min(rows, key=lambda r: abs(r.get("strikePrice",0) - spot))
        atm_strike = atm.get("strikePrice", spot)

        # Check ±5 strikes near ATM
        near = [r for r in rows
                if abs(r.get("strikePrice",0) - atm_strike) <= 5 * 50]

        ce_sc, pe_sb, ce_sb, pe_sc = [], [], [], []
        pe_wall = 0

        for row in near:
            sp  = row.get("strikePrice", 0)
            ce  = row.get("CE") or {}
            pe  = row.get("PE") or {}
            coi = ce.get("openInterest", 0) or 0
            cch = ce.get("changeinOpenInterest", 0) or 0
            cpx = ce.get("change", 0) or 0            # CE premium change vs prev close
            poi = pe.get("openInterest", 0) or 0
            pch = pe.get("changeinOpenInterest", 0) or 0
            ppx = pe.get("change", 0) or 0            # PE premium change vs prev close

            # CE tags — classification requires BOTH the OI direction AND the
            # premium price direction (the standard 2x2 OI-interpretation
            # matrix). OI falling alone is ambiguous: it's Short Cover only
            # if premium rose too; if premium fell instead, that's Long
            # Unwind, which is a different (non-bullish) signal and must
            # NOT be counted as Short Cover. Same logic mirrored for PE.
            if coi > 0 and cch < 0 and cpx > 0: ce_sc.append(sp)   # CE Short Cover (OI down, price up)
            if coi > 0 and cch > 0 and cpx < 0: ce_sb.append(sp)   # CE Short Build (OI up, price down)
            # PE tags
            if poi > 0 and pch > 0 and ppx < 0:
                pe_sb.append(sp)                         # PE Short Build (OI up, price down)
                pe_wall += 1
            if poi > 0 and pch < 0 and ppx > 0: pe_sc.append(sp)   # PE Short Cover (OI down, price up)

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
        strikes = sorted(set(r.get("strikePrice",0) for r in rows))
        ce_map = {r.get("strikePrice",0): (r.get("CE") or {}).get("openInterest",0) or 0 for r in rows}
        pe_map = {r.get("strikePrice",0): (r.get("PE") or {}).get("openInterest",0) or 0 for r in rows}
        max_pain = 0
        min_pain = float("inf")
        for t in strikes:
            pain = sum(max(0,(k-t))*ce_map.get(k,0) + max(0,(t-k))*pe_map.get(k,0) for k in strikes)
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

# ── One-time diagnostic dump (first symbol with real rows each run) ──────────
def dump_sample(sym, data):
    try:
        rec = data.get("records", {})
        rows = rec.get("data", [])
        if not rows:
            print(f"[DIAG] Sample symbol={sym}: no rows in records.data")
            return
        ce = rows[0].get("CE") or {}
        pe = rows[0].get("PE") or {}
        print(f"[DIAG] Sample symbol={sym}  strike={rows[0].get('strikePrice')}")
        print(f"[DIAG] CE keys present: {sorted(ce.keys())}")
        print(f"[DIAG] CE values: openInterest={ce.get('openInterest')!r} "
              f"changeinOpenInterest={ce.get('changeinOpenInterest')!r} "
              f"change={ce.get('change')!r} lastPrice={ce.get('lastPrice')!r}")
        print(f"[DIAG] PE keys present: {sorted(pe.keys())}")
        print(f"[DIAG] PE values: openInterest={pe.get('openInterest')!r} "
              f"changeinOpenInterest={pe.get('changeinOpenInterest')!r} "
              f"change={pe.get('change')!r} lastPrice={pe.get('lastPrice')!r}")
    except Exception as e:
        print(f"[DIAG] Sample dump error for {sym}: {e}")

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
    print("  NSE Signal Alert Bot  v2.1 (GitHub Actions)")
    print("=" * 55)
    print(f"  Symbols: {len(FO_STOCKS)}")
    print(f"  Telegram: {'configured' if TELEGRAM_TOKEN != 'YOUR_BOT_TOKEN' else 'NOT SET'}")
    print("=" * 55)

    if not is_market_open():
        print(f"[{now_ist().strftime('%H:%M')}] Market closed. Skipping scan.")
        return

    active_signals = load_state()

    refresh_nse_session()

    errors = 0
    diag = {
        "fetched_ok": 0, "fetched_empty": 0,
        "analysed_ok": 0, "analysed_none": 0,
        "any_ce_sc": 0, "any_pe_sb": 0, "any_ce_sb": 0, "any_pe_sc": 0,
    }
    sample_dumped = False
    print(f"[{now_ist().strftime('%H:%M:%S')}] Scanning {len(FO_STOCKS)} symbols...")

    # Scan in batches of 10 with small delay between batches
    BATCH = 10
    for i in range(0, len(FO_STOCKS), BATCH):
        batch = FO_STOCKS[i:i+BATCH]
        for sym in batch:
            try:
                data = fetch_option_chain(sym)
                if data:
                    diag["fetched_ok"] += 1
                    if not sample_dumped:
                        dump_sample(sym, data)
                        sample_dumped = True
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
                time.sleep(0.3)
            except Exception:
                errors += 1
        time.sleep(1)  # pause between batches

    print(f"  Done. {len(FO_STOCKS)} symbols. Errors: {errors}.")
    print(f"[DIAG] Fetch OK: {diag['fetched_ok']}  Fetch empty/blocked: {diag['fetched_empty']}")
    print(f"[DIAG] Analysed OK: {diag['analysed_ok']}  Analysed None (no spot/rows): {diag['analysed_none']}")
    print(f"[DIAG] Symbols with any CE-Short-Cover: {diag['any_ce_sc']}  "
          f"any PE-Short-Build: {diag['any_pe_sb']}  "
          f"any CE-Short-Build: {diag['any_ce_sb']}  "
          f"any PE-Short-Cover: {diag['any_pe_sc']}")

    save_state(active_signals)

if __name__ == "__main__":
    run()
