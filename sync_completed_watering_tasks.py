#!/usr/bin/env python3
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests
from dateutil import parser as dateparser

# --- Config (env) ---
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
HOUSEPLANT_DB_ID = os.environ["NOTION_DATABASE_ID"]
THINGS_PROJECT_NAME = os.environ.get("THINGS_PROJECT_NAME", "Plant Care")

# Notion property names (match your database)
PROP_LAST_WATERED = os.environ.get("PROP_LAST_WATERED", "Last Watered")

# --- State ---
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "sync_state.json"

NOTION_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"

NOTION_ID_RE = re.compile(r"notion_id:\s*([0-9a-fA-F-]{32,36})")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def normalize_notion_id(notion_id: str) -> str:
    # Notion accepts both dashed and undashed, but store dashed consistently
    nid = notion_id.strip()
    if len(nid) == 32 and "-" not in nid:
        return f"{nid[0:8]}-{nid[8:12]}-{nid[12:16]}-{nid[16:20]}-{nid[20:32]}"
    return nid


def fetch_recent_logbook_items(limit: int = 800) -> list[dict]:
    """
    Pull recent completed todos from Things Logbook.
    Robust: escapes multiline notes so each record is a single line.
    Returns list of dicts: {tid, notes, completion_str}
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
      if tNotes contains "notion_id:" then
        set tId to (id of t) as text
        set tComp to (completion date of t) as text

        -- Escape line breaks in notes so output stays one record per line
        set tNotes to my replaceText((ASCII character 10), "\\\\n", tNotes)
        set tNotes to my replaceText((ASCII character 13), "", tNotes)

        set outText to outText & tId & "|||" & tNotes & "|||" & tComp & linefeed
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
            tid, notes, comp = line.split("|||", 2)
            # Un-escape notes back to real newlines (optional)
            notes = notes.replace("\\n", "\n")
            items.append({
                "tid": tid.strip(),
                "name": "(name unavailable)",
                "notes": notes,
                "completion_str": comp.strip(),
            })
        except Exception:
            continue

    return items

def extract_notion_id(notes: str) -> str | None:
    m = NOTION_ID_RE.search(notes or "")
    if not m:
        return None
    return normalize_notion_id(m.group(1))


def parse_completion_date(completion_str: str) -> datetime:
    """
    completion_str is locale-ish (e.g. 'Sunday, January 4, 2026 at 10:51:10 AM').
    dateutil can parse this reliably on macOS.
    """
    dt = dateparser.parse(completion_str)
    if dt.tzinfo is None:
        # Treat as local time, convert to UTC ISO for storage/comparison if needed
        # For Notion date property, we can just use YYYY-MM-DD in local date.
        return dt
    return dt.astimezone(timezone.utc)


def notion_update_last_watered(page_id: str, local_date_yyyy_mm_dd: str) -> None:
    url = f"{NOTION_API_BASE}/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "properties": {
            PROP_LAST_WATERED: {"date": {"start": local_date_yyyy_mm_dd}}
        }
    }
    r = requests.patch(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def main():
    state = load_state()
    processed_ids: list[str] = state.get("processed_things_ids", [])
    processed_set = set(processed_ids)

    items = fetch_recent_logbook_items(limit=400)

    to_process = []
    for it in items:
        tid = it["tid"]
        if tid in processed_set:
            continue
        nid = extract_notion_id(it["notes"])
        if not nid:
            continue

        # Use completion date as the watering date
        try:
            comp_dt = parse_completion_date(it["completion_str"])
        except Exception:
            comp_dt = datetime.now()

        local_date = comp_dt.date().isoformat()
        to_process.append((tid, nid, local_date, it["name"]))

    print(f"Found {len(to_process)} new Logbook items with notion_id in last 400 entries.")

    updated = 0
    for tid, notion_page_id, local_date, name in to_process:
        try:
            notion_update_last_watered(notion_page_id, local_date)
            updated += 1
            print(f"Updated Notion Last Watered = {local_date} for: {name}")
        except Exception as e:
            print(f"FAILED updating Notion for {name} ({notion_page_id}): {e}")

        processed_ids.append(tid)

    # Cap processed list so it doesn't grow forever
    processed_ids = processed_ids[-2000:]
    state["processed_things_ids"] = processed_ids
    state["last_run_iso"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print(f"Processed {updated} updates. State saved to {STATE_FILE}")


if __name__ == "__main__":
    main()
