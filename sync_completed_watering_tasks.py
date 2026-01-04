#!/usr/bin/env python3
import os
import re
import json
import subprocess
import datetime as dt
from typing import Dict, List, Any

import requests

# ===== Config (from .env) =====
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
PROP_LAST_WATERED = os.environ.get("PROP_LAST_WATERED", "Last Watered")

NOTION_VERSION = "2022-06-28"

NOTION_ID_RE = re.compile(r"notion_id:\s*([0-9a-fA-F-]{32,36})")
SYNCED_RE = re.compile(r"synced:\s*yes", re.IGNORECASE)

STATE_DIR = os.path.expanduser("~/plantbot/state")
STATE_FILE = os.path.join(STATE_DIR, "things_logbook_sync_state.json")

def notion_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

def ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)

def load_last_sync_iso() -> str:
    ensure_state_dir()
    if not os.path.exists(STATE_FILE):
        return (dt.datetime.now() - dt.timedelta(days=7)).replace(microsecond=0).isoformat()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("last_sync_iso") or (dt.datetime.now() - dt.timedelta(days=7)).replace(microsecond=0).isoformat()

def save_last_sync_iso(ts_iso: str) -> None:
    ensure_state_dir()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_sync_iso": ts_iso}, f)

def fetch_recent_unsynced_logbook_items(since_iso: str) -> List[Dict[str, Any]]:
    applescript = f'''
use framework "Foundation"
use scripting additions

set sinceISO to "{escape_applescript(since_iso)}"

set isoParser to current application's NSDateFormatter's new()
isoParser's setLocale:(current application's NSLocale's localeWithLocaleIdentifier:"en_US_POSIX")
isoParser's setDateFormat:"yyyy-MM-dd'T'HH:mm:ss"

set sinceDate to isoParser's dateFromString:sinceISO
if sinceDate is missing value then
  set sinceDate to (current application's NSDate's dateWithTimeIntervalSinceNow:(-604800)) -- 7d
end if

set isoOut to current application's NSDateFormatter's new()
isoOut's setLocale:(current application's NSLocale's localeWithLocaleIdentifier:"en_US_POSIX")
isoOut's setDateFormat:"yyyy-MM-dd'T'HH:mm:ss"

tell application "Things3"
  set lb to to dos of list "Logbook"
  set outItems to {{}}

  repeat with t in lb
    try
      set nt to notes of t
      if nt is missing value then set nt to ""

      if nt contains "notion_id:" then
        if not (nt contains "synced: yes") then
          set cd to completion date of t
          if cd is not missing value then
            set cdNSDate to current application's NSDate's dateWithTimeIntervalSince1970:(cd - date "Thursday, January 1, 1970 00:00:00" as number)
            if (cdNSDate's compare:sinceDate) = 1 then
              set cdISO to (isoOut's stringFromDate:cdNSDate) as text
              set end of outItems to {{tid:(id of t as text), nm:(name of t as text), nt:(nt as text), completionISO:cdISO}}
            end if
          end if
        end if
      end if
    end try
  end repeat
end tell

set jsonArray to current application's NSMutableArray's new()
repeat with r in outItems
  set d to current application's NSMutableDictionary's new()
  -- IMPORTANT: r is a record we created, not a Things object
  d's setObject:(tid of r) forKey:"id"
  d's setObject:(nm of r) forKey:"name"
  d's setObject:(nt of r) forKey:"notes"
  d's setObject:(completionISO of r) forKey:"completionISO"
  jsonArray's addObject:d
end repeat

set jsonData to current application's NSJSONSerialization's dataWithJSONObject:jsonArray options:0 |error|:(missing value)
set jsonStr to (current application's NSString's alloc()'s initWithData:jsonData encoding:(current application's NSUTF8StringEncoding)) as text
return jsonStr
'''
    p = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "osascript failed")

    raw = p.stdout.strip()
    if not raw:
        return []
    return json.loads(raw)

def mark_synced_in_things_by_id(task_id: str, current_notes: str) -> None:
    new_notes = (current_notes.rstrip() + "\n\nsynced: yes").strip()
    applescript = f'''
tell application "Things3"
  set t to to do id "{escape_applescript(task_id)}"
  set notes of t to "{escape_applescript(new_notes)}"
end tell
'''
    subprocess.run(["osascript", "-e", applescript], check=True)

def update_last_watered(notion_page_id: str, completion_iso: str) -> None:
    date_only = completion_iso.split("T", 1)[0]
    url = f"https://api.notion.com/v1/pages/{notion_page_id}"
    payload = {
        "properties": {
            PROP_LAST_WATERED: {"date": {"start": date_only}}
        }
    }
    r = requests.patch(url, headers=notion_headers(), json=payload, timeout=30)
    r.raise_for_status()

def main() -> None:
    since_iso = load_last_sync_iso()
    now_iso = dt.datetime.now().replace(microsecond=0).isoformat()

    items = fetch_recent_unsynced_logbook_items(since_iso)
    print(f"Since {since_iso}, found {len(items)} unsynced Logbook items with notion_id:")

    processed = 0
    for it in items:
        task_id = it.get("id", "") or ""
        name = it.get("name", "") or ""
        notes = it.get("notes", "") or ""
        completion_iso = it.get("completionISO", "") or ""

        if not task_id or not completion_iso:
            continue
        if SYNCED_RE.search(notes):
            continue

        m = NOTION_ID_RE.search(notes)
        if not m:
            continue

        notion_id = m.group(1)
        print(f"SYNC: {name} -> {notion_id} (Last Watered = {completion_iso.split('T',1)[0]})")

        update_last_watered(notion_id, completion_iso)
        mark_synced_in_things_by_id(task_id, notes)
        processed += 1

    save_last_sync_iso(now_iso)
    print(f"Processed {processed} tasks. Saved last_sync_iso = {now_iso}")

if __name__ == "__main__":
    main()
