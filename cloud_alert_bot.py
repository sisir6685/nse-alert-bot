"""
reverse_signal_log.py — ONE-TIME cleanup for signal_log.csv

Your existing signal_log.csv has ~10,383 lines written in OLDEST-FIRST
order (each old run just appended to the bottom). The bot's log_signal()
has now been fixed to insert NEW rows at the TOP going forward. This
script bridges the gap: it reverses all EXISTING rows once, so the whole
file becomes newest-first from top to bottom, consistent with how new
rows will be added from now on.

Run this ONCE, right before you push/re-run the fixed bot. Do not run
it again after that — running it twice will just re-reverse everything
back to oldest-first.

HOW TO USE:
  1. Download signal_log.csv from your repo (or clone the repo).
  2. Put this script in the same folder as signal_log.csv.
  3. Run:  python3 reverse_signal_log.py
  4. It creates a backup (signal_log_backup_<timestamp>.csv) automatically
     before touching anything, then rewrites signal_log.csv reversed.
  5. Review signal_log.csv — first data row should now be your MOST
     RECENT signal, last row your OLDEST.
  6. Commit the updated signal_log.csv back to your repo (git add, commit,
     push) so GitHub Actions picks up the corrected order.
"""

import os
import csv
import shutil
from datetime import datetime

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log.csv")

def main():
    if not os.path.exists(LOG_FILE):
        print(f"signal_log.csv not found at: {LOG_FILE}")
        print("Put this script in the same folder as signal_log.csv and re-run.")
        return

    with open(LOG_FILE, "r", newline="") as f:
        rows = list(csv.reader(f))

    if not rows:
        print("signal_log.csv is empty — nothing to reverse.")
        return

    header, data_rows = rows[0], rows[1:]
    print(f"Found {len(data_rows)} data rows (plus header).")

    if not data_rows:
        print("No data rows to reverse — only a header present.")
        return

    # Backup first — never touch the original without a safety copy.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(os.path.dirname(LOG_FILE), f"signal_log_backup_{ts}.csv")
    shutil.copy2(LOG_FILE, backup_path)
    print(f"Backup saved: {backup_path}")

    reversed_rows = list(reversed(data_rows))

    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(reversed_rows)

    print("Done. signal_log.csv reversed — newest signal is now the first data row.")
    print()
    print("First 3 lines after reversal:")
    with open(LOG_FILE, "r") as f:
        for i, line in enumerate(f):
            if i > 3:
                break
            print(" ", line.rstrip())
    print()
    print("Next step: commit and push this file so GitHub Actions uses the corrected order.")

if __name__ == "__main__":
    main()
