"""
analyze_oi_tracking.py
=======================
Reads oi_tracking_log.csv (built up daily by cloud_alert_bot.py's
track_oi_history()) and reports, for each classification (Long Buildup /
Short Buildup / Long Unwinding / Short Covering), how often the NEXT
day's price move actually continued in the expected direction, broken
down by OI% change magnitude bucket.

This is what answers the "what OI% change can I expect the rally to
continue at" question empirically, using your own historical data,
rather than a generic rule of thumb.

Usage:
    python analyze_oi_tracking.py
    python analyze_oi_tracking.py --file oi_tracking_log.csv --min-rows 5

Expects the OI_LOG_FILE columns to match cloud_alert_bot.py's
OI_HEADER_ROW: date, symbol, oi_pct_change, price_pct_change,
classification, next_day_price_change_pct
"""

import csv
import argparse
import os

# Bullish classifications: a "continuation" is next-day price moving UP.
# Bearish classifications: a "continuation" is next-day price moving DOWN.
BULLISH = {"Long Buildup", "Short Covering"}
BEARISH = {"Short Buildup", "Long Unwinding"}

# OI% change magnitude buckets — edit these to taste once you see the
# actual distribution of your data (run once with defaults first).
BUCKETS = [
    (0.0, 0.5, "0-0.5%"),
    (0.5, 1.0, "0.5-1%"),
    (1.0, 2.0, "1-2%"),
    (2.0, 5.0, "2-5%"),
    (5.0, float("inf"), "5%+"),
]

def bucket_for(oi_pct_abs):
    for lo, hi, label in BUCKETS:
        if lo <= oi_pct_abs < hi:
            return label
    return "?"

def load_rows(path):
    rows = []
    if not os.path.exists(path):
        print(f"[ERROR] File not found: {path}")
        return rows
    with open(path, "r", newline="") as f:
        for r in csv.DictReader(f):
            try:
                if r["classification"] not in (BULLISH | BEARISH):
                    continue  # skip "Flat" / "N/A" rows — no directional read to test
                oi_pct = float(r["oi_pct_change"])
                outcome = float(r["next_day_price_change_pct"])
                rows.append({
                    "date": r["date"], "symbol": r["symbol"],
                    "classification": r["classification"],
                    "oi_pct_abs": abs(oi_pct),
                    "outcome": outcome,
                })
            except (ValueError, KeyError):
                continue  # blank/incomplete row (e.g. first-ever day for a symbol)
    return rows

def report(rows, min_rows):
    # Group by (classification, bucket)
    groups = {}
    for r in rows:
        key = (r["classification"], bucket_for(r["oi_pct_abs"]))
        groups.setdefault(key, []).append(r)

    print(f"{'Classification':<16} {'OI% Bucket':<10} {'N':>5} {'Win Rate':>9} {'Avg Outcome':>12}")
    print("-" * 60)

    # Stable, readable ordering: classification, then bucket order as defined above.
    bucket_order = [b[2] for b in BUCKETS]
    class_order = ["Long Buildup", "Short Covering", "Short Buildup", "Long Unwinding"]

    for cls in class_order:
        is_bullish = cls in BULLISH
        for label in bucket_order:
            key = (cls, label)
            items = groups.get(key, [])
            if len(items) < min_rows:
                continue
            n = len(items)
            wins = sum(1 for r in items if (r["outcome"] > 0) == is_bullish)
            win_rate = wins / n * 100
            avg_outcome = sum(r["outcome"] for r in items) / n
            print(f"{cls:<16} {label:<10} {n:>5} {win_rate:>8.1f}% {avg_outcome:>11.2f}%")

    skipped = [k for k, v in groups.items() if len(v) < min_rows]
    if skipped:
        print(f"\n({len(skipped)} classification/bucket combos skipped — fewer than "
              f"{min_rows} rows so far; run this again after more sessions accumulate.)")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="oi_tracking_log.csv")
    ap.add_argument("--min-rows", type=int, default=5,
                     help="Minimum rows in a bucket before it's reported (avoids noisy single-digit-N stats)")
    args = ap.parse_args()

    rows = load_rows(args.file)
    print(f"Loaded {len(rows)} directional rows from {args.file}\n")
    if not rows:
        print("No usable rows yet — this needs at least a few weeks of the bot running "
              "(oi_tracking_log.csv builds one row per symbol per day, once 2+ prior "
              "trading days of data exist for that symbol).")
    else:
        report(rows, args.min_rows)
