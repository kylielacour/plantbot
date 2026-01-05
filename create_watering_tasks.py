#!/usr/bin/env python3
import os
import subprocess
import datetime as dt
import requests
from typing import Dict, Any, Optional

# ===== Config (from .env) =====
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
THINGS_PROJECT = os.environ.get("THINGS_PROJECT", "Plant Care")

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
def notion_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

def get_title(props: Dict[str, Any]) -> str:
    p = props.get(PROP_NAME)
    if p and p.get("type") == "title":
        return "".join(x.get("plain_text", "") for x in p.get("title", [])).strip()

    for _, v in props.items():
        if v.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in v.get("title", [])).strip()

    return "Untitled Plant"

def get_number(props: Dict[str, Any], name: str) -> Optional[float]:
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
def task_exists_for_notion_id(page_id: str) -> bool:
    applescript = f'''
tell application "Things3"
  tell project "{escape(THINGS_PROJECT)}"
    return exists (to dos whose notes contains "notion_id: {page_id}" and status is open)
  end tell
end tell
'''
    p = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    return "true" in (p.stdout or "").lower()

def create_things_task_due_today(title: str, notes: str) -> None:
    applescript = f'''
tell application "Things3"
  tell project "{escape(THINGS_PROJECT)}"
    set newTodo to make new to do
    set name of newTodo to "{escape(title)}"
    set notes of newTodo to "{escape(notes)}"
    set due date of newTodo to (current date)
  end tell
end tell
'''
    subprocess.run(["osascript", "-e", applescript], check=True)

# ===== Main =====
def main() -> None:
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

    for page in results:
        page_id = page["id"]
        props = page.get("properties", {})

        if task_exists_for_notion_id(page_id):
            continue

        plant_name = get_title(props)

        ml = get_number(props, PROP_RECOMMENDED_WATER)
        if ml is not None:
            ml_rounded = int(round(ml))
            ml_str = f"{ml_rounded} ml"
            title_amount = ml_to_cups_str(float(ml_rounded))
        else:
            ml_str = "ml?"
            title_amount = "amount?"

        task_title = f"Water {plant_name} — {title_amount}"

        notes = (
            f"Amount: {ml_str}\n"
            f"Notion: https://www.notion.so/{page_id.replace('-', '')}\n"
            f"notion_id: {page_id}"
        )

        create_things_task_due_today(task_title, notes)

if __name__ == "__main__":
    main()
