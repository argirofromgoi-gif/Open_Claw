#!/usr/bin/env python3
"""
Fill missing dates in the Chris Fountoulis content calendar.
Assigns Tue/Thu schedule starting from today, skipping rows that already have a date.

Usage:
    python3 fill_cf_dates.py            # preview dates
    python3 fill_cf_dates.py --apply    # write dates to Google Sheet
"""

import sys
import json
import os
import requests
from datetime import datetime, timedelta

TOKENS_DIR = "/home/ubuntu/tokens"
CF_SPREADSHEET_ID = "178jvepS4CDfjId7rgvCZOBzv5GQ-Sey0ASSMQJctd0o"


def _refresh_token(token_file: str) -> str | None:
    try:
        with open(token_file) as f:
            data = json.load(f)
        resp = requests.post(data["token_uri"], data={
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
            "refresh_token": data["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=15)
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:
        print(f"  Token refresh failed for {token_file}: {exc}")
        return None


def _get_access_token() -> str:
    try:
        token_files = [
            os.path.join(TOKENS_DIR, f)
            for f in os.listdir(TOKENS_DIR)
            if f.endswith(".json")
        ]
    except FileNotFoundError:
        raise RuntimeError(f"Tokens directory not found: {TOKENS_DIR}")
    for tf in sorted(token_files):
        token = _refresh_token(tf)
        if token:
            return token
    raise RuntimeError("All token files failed to refresh.")


def _read_sheet(access_token: str) -> list[list[str]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{CF_SPREADSHEET_ID}/values/A:B",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("values", [])


def _update_cell(access_token: str, cell: str, value: str) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    resp = requests.put(
        f"https://sheets.googleapis.com/v4/spreadsheets/{CF_SPREADSHEET_ID}/values/{cell}",
        headers=headers,
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": [[value]]},
        timeout=15,
    )
    resp.raise_for_status()


def generate_schedule(count: int) -> list[str]:
    """Generate `count` Mon/Wed/Fri/Sun dates starting from today, in DD/MM/YYYY format."""
    # Weekday numbers: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    target_days = {0, 2, 4, 6}  # Monday, Wednesday, Friday, Sunday
    dates = []
    current = datetime.now()
    # Start from today if it's a target day, otherwise advance to the next one
    if current.weekday() not in target_days:
        days_ahead = min((d - current.weekday()) % 7 for d in target_days)
        current += timedelta(days=days_ahead)
    while len(dates) < count:
        if current.weekday() in target_days:
            dates.append(current.strftime("%d/%m/%Y"))
        current += timedelta(days=1)
    return dates


def main():
    apply = "--apply" in sys.argv

    print("Reading CF Content Calendar...")
    access_token = _get_access_token()
    rows = _read_sheet(access_token)

    today = datetime.now().date()

    # Find rows where date is empty or in the past
    empty_rows = []
    for i, row in enumerate(rows):
        cell_date = row[0].strip() if len(row) > 0 else ""
        topic     = row[1].strip() if len(row) > 1 else "(no topic)"
        if i == 0 and cell_date.lower() in ("date", "ημερομηνία", ""):
            continue  # skip header
        needs_date = not cell_date
        if cell_date:
            try:
                if datetime.strptime(cell_date, "%d/%m/%Y").date() < today:
                    needs_date = True
            except ValueError:
                needs_date = True
        if needs_date:
            empty_rows.append((i + 1, topic, cell_date or "—"))

    if not empty_rows:
        print("All rows already have future dates. Nothing to do.")
        return

    print(f"\nFound {len(empty_rows)} rows that need a new date.\n")
    schedule = generate_schedule(len(empty_rows))

    print(f"{'Row':<6} {'Old date':<14} {'New date':<14} Topic")
    print("-" * 80)
    updates = []
    for (row_idx, topic, old_date), date_str in zip(empty_rows, schedule):
        print(f"{row_idx:<6} {old_date:<14} {date_str:<14} {topic}")
        updates.append((f"A{row_idx}", date_str))

    if not apply:
        print("\nPreview only. Run with --apply to write these dates to the sheet.")
        return

    print("\nWriting dates to Google Sheet...")
    for cell, value in updates:
        _update_cell(access_token, cell, value)
        print(f"  ✅ {cell} = {value}")

    print(f"\nDone. {len(updates)} dates written.")


if __name__ == "__main__":
    main()
