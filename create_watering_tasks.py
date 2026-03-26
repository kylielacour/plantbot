#!/usr/bin/env python3
import os
import re
import subprocess
import datetime as dt
from typing import Any

import requests

# ===== Config (from .env) =====
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
THINGS_PROJECT = os.environ.get("THINGS_PROJECT_NAME", "Plant Care")

PROP_NAME = os.environ.get("PROP_NAME", "Name")
PROP_NEXT_WATERING = os.environ.get("PROP_NEXT_WATERING", "Next Watering")
PROP_RECOMMENDED_WATER = os.environ.get("PROP_RECOMMENDED_WATER", "Recommended Water (ml)")

NOTION_VERSION = "2022-06-28"

# ===== Cups (fractions) =====
ML_PER_CUP = 236.588
FRACTIONS = [
    (0.0, ""),
    (0.25, "¼"),
    (1 / 3, "⅓"),
    (0.5, "½"),
    (2 / 3, "⅔"),
    (0.75, "¾"),
    (1.0, ""),
]

def ml_to_cups_str(ml: float) -> str:
    cups = ml / ML_PER_CUP
    whole = int(cups)
    remainder = cups - whole

    frac_value, frac_str = min(FRACTIONS, key=lambda f: abs(remainder - f[0]))

    if frac_value >= 0.99:
        whole += 1
        frac_str = ""

    if whole == 0 and frac_str:
        return f"{frac_str} cup"
    if whole > 0 and frac_str:
        return f"{whole}{frac_str} cups"
    if whole > 0:
        return f"{whole} cup" if whole == 1 else f"{whole} cups"
    return "0 cups"

# ===== Helpers =====
def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

def get_title(props: dict[str, Any]) -> str:
    p = props.get(PROP_NAME)
    if p and p.get("type") == "title":
        return "".join(x.get("plain_text", "") for x in p.get("title", [])).strip()

    for _, v in props.items():
        if v.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in v.get("title", [])).strip()

    return "Untitled Plant"

def get_date(props: dict[str, Any], name: str) -> str | None:
    p = props.get(name)
    if not p:
        return None
    if p.get("type") == "date" and p.get("date"):
        return p["date"].get("start")
    if p.get("type") == "formula":
        f = p.get("formula", {})
        if f.get("type") == "date" and f.get("date"):
            return f["date"].get("start")
    return None

def get_number(props: dict[str, Any], name: str) -> float | None:
    p = props.get(name)
    if not p:
        return None

    t = p.get("type")
    if t == "number":
        return p.get("number")

    if t == "formula":
        f = p.get("formula", {})
        if f.get("type") == "number":
            return f.get("number")

    if t == "rollup":
        r = p.get("rollup", {})
        if r.get("type") == "number":
            return r.get("number")

    return None

# ===== Things (dedupe by notion_id) =====
def get_open_notion_ids() -> set[str]:
    applescript = f'''
tell application "Things3"
  tell project "{escape(THINGS_PROJECT)}"
    set ids to {{}}
    repeat with t in (to dos whose status is open)
      set n to notes of t
      if n contains "notion_id:" then
        set end of ids to n
      end if
    end repeat
    return ids
  end tell
end tell
'''
    p = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    return {m.group(1) for m in re.finditer(r"notion_id:\s*([\w-]+)", p.stdout or "")}


def get_recently_completed_notion_ids(days: int = 2) -> set[str]:
    """Check Logbook for tasks completed within the last N days to avoid
    re-creating a task before the sync script updates Notion."""
    applescript = f'''
tell application "Things3"
  set cutoff to (current date) - ({days} * days)
  set lb to to dos of list "Logbook"
  set ids to {{}}
  set maxIndex to 200
  set n to count of lb
  if maxIndex > n then set maxIndex to n
  repeat with i from 1 to maxIndex
    set t to item i of lb
    try
      if (completion date of t) > cutoff then
        set tn to notes of t
        if tn contains "notion_id:" then
          set end of ids to tn
        end if
      end if
    end try
  end repeat
  return ids
end tell
'''
    p = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    return {m.group(1) for m in re.finditer(r"notion_id:\s*([\w-]+)", p.stdout or "")}

def create_things_task(title: str, notes: str, days_offset: int = 0) -> None:
    applescript = f'''
tell application "Things3"
  tell project "{escape(THINGS_PROJECT)}"
    set newTodo to make new to do
    set name of newTodo to "{escape(title)}"
    set notes of newTodo to "{escape(notes)}"
    set due date of newTodo to ((current date) + ({days_offset} * days))
  end tell
end tell
'''
    subprocess.run(["osascript", "-e", applescript], check=True)

# ===== Main =====
def main() -> None:
    print("Running create_watering_tasks...")
    print("THINGS_PROJECT:", THINGS_PROJECT)
    print("NOTION_DATABASE_ID:", NOTION_DATABASE_ID)

    today_iso = dt.date.today().isoformat()

    payload = {
        "filter": {
            "property": PROP_NEXT_WATERING,
            "date": {"on_or_before": today_iso},
        },
        "page_size": 100,
    }

    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    r = requests.post(url, headers=notion_headers(), json=payload, timeout=30)
    r.raise_for_status()

    results = r.json().get("results", [])

    print(f"Notion rows returned: {len(results)}")

    open_notion_ids = get_open_notion_ids()
    recent_notion_ids = get_recently_completed_notion_ids(days=2)
    skip_ids = open_notion_ids | recent_notion_ids

    for page in results:
        page_id = page["id"]
        props = page.get("properties", {})

        if page_id in skip_ids:
            reason = "open task" if page_id in open_notion_ids else "recently completed"
            print(f"SKIP ({reason}):", page_id)
            continue

        plant_name = get_title(props)

        ml = get_number(props, PROP_RECOMMENDED_WATER)
        if ml is not None:
            ml_rounded = int(round(ml))
            ml_str = f"{ml_rounded} ml"
            title_amount = ml_to_cups_str(ml_rounded)
        else:
            ml_str = "ml?"
            title_amount = "amount?"

        task_title = f"Water {plant_name} — {title_amount}"

        notes = (
            f"Amount: {ml_str}\n"
            f"Notion: https://www.notion.so/{page_id.replace('-', '')}\n"
            f"notion_id: {page_id}"
        )

        next_water_str = get_date(props, PROP_NEXT_WATERING)
        if next_water_str:
            due_date = dt.date.fromisoformat(next_water_str[:10])
            days_offset = (due_date - dt.date.today()).days
        else:
            days_offset = 0

        print("READY:", task_title)

        create_things_task(task_title, notes, days_offset)

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
