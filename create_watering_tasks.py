#!/usr/bin/env python3
"""Create watering tasks in Things from local plant config.

Flow:
  1. Load plants from plants.yaml.
  2. Get current climate (Home Assistant thermostat if available, else static).
  3. For each plant, pull species setpoints from the Open Plantbook cache and
     compute interval + amount with watering_model.
  4. If the plant is due (next_date <= today) and not already scheduled/recently
     watered, create a Things task.

Use --dry-run to print the full computed schedule without touching Things.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess

import climate
import plantstore
import watering_model
from openplantbook import OpenPlantbook
from plantstore import make_stores
from units import ml_to_cups_str

THINGS_PROJECT = os.environ.get("THINGS_PROJECT_NAME", "Plant Care")
LATITUDE = float(os.environ.get("LATITUDE", "40"))


def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ===== Things (dedupe by plant_id) =====
# Scan the built-in lists rather than the project. A task that failed to get
# filed into the project is still a real open task, and dedupe must see it --
# scoping this to the project is what let duplicates through. The lists overlap
# (Today is a subset of Anytime); harmless, since we collect into a set.
# App-wide `to dos whose status is open` also works but takes ~100s; this is ~5s.
_OPEN_LISTS = ["Inbox", "Today", "Anytime", "Upcoming", "Someday"]


def get_open_plant_ids() -> set[str]:
    lists = ", ".join(f'"{escape(name)}"' for name in _OPEN_LISTS)
    applescript = f'''
tell application "Things3"
  set ids to {{}}
  repeat with lname in {{{lists}}}
    try
      repeat with t in (to dos of list lname)
        set n to notes of t
        if n contains "plant_id:" then
          set end of ids to n
        end if
      end repeat
    end try
  end repeat
  return ids
end tell
'''
    p = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    if p.returncode != 0:
        # Never fall through to "no open tasks" on an error -- that creates
        # duplicates on every run. Fail loudly instead.
        raise RuntimeError(f"Could not read open tasks from Things: {p.stderr.strip()}")
    return {m.group(1) for m in re.finditer(r"plant_id:\s*([\w-]+)", p.stdout or "")}


def get_recently_completed_plant_ids(days: int = 2) -> set[str]:
    """Logbook items completed within the last N days, so we don't re-create a
    task before the sync script records the watering."""
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
        if tn contains "plant_id:" then
          set end of ids to tn
        end if
      end if
    end try
  end repeat
  return ids
end tell
'''
    p = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
    return {m.group(1) for m in re.finditer(r"plant_id:\s*([\w-]+)", p.stdout or "")}


def create_things_task(title: str, notes: str, days_offset: int = 0) -> None:
    # `make new to do` inside `tell project ...` does NOT file the task into
    # that project -- it lands loose in Anytime with no project. The project has
    # to be assigned explicitly afterwards.
    applescript = f'''
tell application "Things3"
  set newTodo to make new to do
  set name of newTodo to "{escape(title)}"
  set notes of newTodo to "{escape(notes)}"
  set due date of newTodo to ((current date) + ({days_offset} * days))
  set project of newTodo to project "{escape(THINGS_PROJECT)}"
end tell
'''
    subprocess.run(["osascript", "-e", applescript], check=True)


# ===== Main =====
def main(dry_run: bool = False) -> None:
    today = dt.date.today()

    entries, state = make_stores()
    opb = OpenPlantbook.from_env()

    print(f"Plant source: {os.environ.get('PLANTBOT_SERVER_URL') or 'local plants.yaml'}")

    source, source_desc = climate.from_env()
    conditions = source.conditions(today)

    print(f"Climate: {source_desc} -> {conditions.temp_c:.1f}C / "
          f"{conditions.humidity_pct:.0f}% RH"
          + (f" / {conditions.lux:.0f} lux" if conditions.lux else ""))
    print(f"Plants: {len(entries)}  |  Open Plantbook: "
          f"{'enabled' if opb else 'not configured (using fallbacks)'}")

    if dry_run:
        open_ids: set[str] = set()
        recent_ids: set[str] = set()
    else:
        open_ids = get_open_plant_ids()
        recent_ids = get_recently_completed_plant_ids(days=2)

    for entry in entries:
        plant = entry.plant

        base_species = None
        if opb and entry.pid:
            # Fetches on demand and warms the cache; returns None (quietly, and
            # only once) for a species OPB simply doesn't carry.
            try:
                base_species = opb.species_data_or_none(entry.pid)
                if base_species is None:
                    print(f"  note: no Open Plantbook entry for '{entry.pid}'"
                          f" -- using per-plant overrides/defaults")
            except Exception as e:
                print(f"  ! OPB fetch failed for {entry.pid}: {e}")
        species = entry.species_with_overrides(base_species)

        last_watered = state.get_last_watered(plant.id)
        rec = watering_model.watering_recommendation(
            plant=plant,
            species=species,
            conditions=conditions,
            last_watered=last_watered,
            today=today,
            latitude_deg=LATITUDE,
        )

        amount_str = ml_to_cups_str(rec.amount_ml)
        ml_rounded = int(round(rec.amount_ml))
        due_in = (rec.next_date - today).days
        warn = f"  ⚠ {'; '.join(rec.warnings)}" if rec.warnings else ""

        print(f"\n{plant.name} [{plant.id}]")
        print(f"  {rec.explanation}")
        print(f"  amount: {ml_rounded} ml ({amount_str})  "
              f"last watered: {last_watered or 'never'}  "
              f"next: {rec.next_date} (in {due_in}d){warn}")

        if due_in > 0:
            continue  # not due yet

        if plant.id in open_ids:
            print("  SKIP (open task already exists)")
            continue
        if plant.id in recent_ids:
            print("  SKIP (watered recently, sync pending)")
            continue

        title = f"Water {plant.name} — {amount_str}"
        notes = (
            f"Amount: {ml_rounded} ml\n"
            f"{rec.explanation}\n"
            f"plant_id: {plant.id}"
        )

        if dry_run:
            print("  DRY-RUN: would create task:", title)
        else:
            print("  CREATE:", title)
            create_things_task(title, notes, days_offset=0)


def notify_error(message: str) -> None:
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "plantbot" sound name "Basso"',
    ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create watering tasks in Things.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the computed schedule without creating tasks")
    args = parser.parse_args()

    try:
        main(dry_run=args.dry_run)
    except Exception as e:
        notify_error(f"Error: {e}")
        raise
