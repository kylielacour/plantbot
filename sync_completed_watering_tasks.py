#!/usr/bin/env python3
"""Sync completed Things watering tasks back into local state.

Watches the Things Logbook for completed tasks carrying a ``plant_id:`` note and
records the completion date as that plant's ``last_watered`` in
state/watering_state.json (previously this PATCHed Notion).
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dateutil import parser as dateparser

import plantstore

# --- State ---
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "sync_state.json"

PLANT_ID_RE = re.compile(r"plant_id:\s*([\w-]+)")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def fetch_recent_logbook_items(limit: int = 400) -> list[dict]:
    """Pull recent completed todos from the Things Logbook.

    Escapes multiline notes so each record stays on a single output line.
    Returns list of dicts: {tid, name, notes, completion_str}.
    """
    applescript = f'''
on replaceText(find, repl, theText)
  set AppleScript's text item delimiters to find
  set parts to text items of theText
  set AppleScript's text item delimiters to repl
  set theText to parts as text
  set AppleScript's text item delimiters to ""
  return theText
end replaceText

tell application "Things3"
  set lb to to dos of list "Logbook"
  set outText to ""
  set n to count of lb
  set maxIndex to {limit}
  if maxIndex > n then set maxIndex to n

  repeat with i from 1 to maxIndex
    set t to item i of lb
    try
      set tNotes to (notes of t)
      if tNotes contains "plant_id:" then
        set tId to (id of t) as text
        set tName to (name of t) as text
        set tComp to (completion date of t) as text

        set tNotes to my replaceText((ASCII character 10), "\\\\n", tNotes)
        set tNotes to my replaceText((ASCII character 13), "", tNotes)
        set tName to my replaceText((ASCII character 10), " ", tName)
        set tName to my replaceText((ASCII character 13), "", tName)

        set outText to outText & tId & "|||" & tNotes & "|||" & tComp & "|||" & tName & linefeed
      end if
    end try
  end repeat

  return outText
end tell
'''
    p = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "osascript failed")

    raw = p.stdout.strip()
    if not raw:
        return []

    items = []
    for line in raw.splitlines():
        if "|||" not in line:
            continue
        try:
            parts = line.split("|||")
            items.append({
                "tid": parts[0].strip(),
                "notes": parts[1].replace("\\n", "\n"),
                "completion_str": parts[2].strip(),
                "name": parts[3].strip() if len(parts) > 3 else "(name unavailable)",
            })
        except Exception:
            continue

    return items


def extract_plant_id(notes: str) -> str | None:
    m = PLANT_ID_RE.search(notes or "")
    return m.group(1) if m else None


def parse_completion_date(completion_str: str) -> datetime:
    """completion_str is locale-ish, e.g. 'Sunday, January 4, 2026 at 10:51 AM'."""
    return dateparser.parse(completion_str)


def main():
    state = load_state()
    processed_ids: list[str] = state.get("processed_things_ids", [])
    processed_set = set(processed_ids)

    _, watering_state = plantstore.make_stores()

    items = fetch_recent_logbook_items(limit=400)

    to_process = []
    for it in items:
        tid = it["tid"]
        if tid in processed_set:
            continue
        pid = extract_plant_id(it["notes"])
        if not pid:
            continue
        try:
            comp_dt = parse_completion_date(it["completion_str"])
        except Exception:
            comp_dt = datetime.now()
        to_process.append((tid, pid, comp_dt.date(), it["name"]))

    print(f"Found {len(to_process)} new Logbook waterings with plant_id.")

    updated = 0
    for tid, plant_id, local_date, name in to_process:
        try:
            watering_state.set_last_watered(plant_id, local_date)
            processed_ids.append(tid)
            updated += 1
            print(f"last_watered[{plant_id}] = {local_date.isoformat()}  ({name})")
        except Exception as e:
            print(f"FAILED to record {plant_id} ({name}): {e} — will retry next run")

    # Cap processed list so it doesn't grow forever.
    processed_ids = processed_ids[-2000:]
    state["processed_things_ids"] = processed_ids
    state["last_run_iso"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print(f"Recorded {updated} waterings. State saved to {STATE_FILE}")


def notify_error(message: str) -> None:
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "plantbot" sound name "Basso"',
    ])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify_error(f"Error: {e}")
        raise
