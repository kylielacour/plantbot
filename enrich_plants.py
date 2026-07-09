#!/usr/bin/env python3
"""Resolve Open Plantbook species ids for your plants and warm the cache.

For each plant in plants.yaml:
  * if it already has a `pid`, fetch + cache that species' detail;
  * if not, search OPB by the plant name and print candidate pids so you can
    paste the right one into plants.yaml.

Run once after editing plants.yaml (and again whenever you add plants):
    ./venv/bin/python enrich_plants.py
"""
from __future__ import annotations

import plantstore
from openplantbook import OpenPlantbook


def main() -> None:
    opb = OpenPlantbook.from_env()
    if opb is None:
        print("Open Plantbook not configured — set OPB_CLIENT_ID / "
              "OPB_CLIENT_SECRET in .env. Skipping.")
        return

    entries = plantstore.load_plants()
    for entry in entries:
        plant = entry.plant
        if entry.pid:
            try:
                sd = opb.species_data(entry.pid, refresh=True)
                print(f"[cached] {plant.name}  pid='{entry.pid}'  "
                      f"soil_moist={sd.min_soil_moist}-{sd.max_soil_moist}%  "
                      f"light={sd.min_light_lux}-{sd.max_light_lux} lux")
            except Exception as e:
                print(f"[error]  {plant.name}  pid='{entry.pid}': {e}")
            continue

        # No pid yet — search and suggest.
        query = plant.name
        try:
            results = opb.search(query)
        except Exception as e:
            print(f"[error]  search '{query}': {e}")
            continue

        if not results:
            print(f"[no match] {plant.name} — try a Latin name in plants.yaml")
            continue

        print(f"[choose] {plant.name}: add one of these as `pid:` in plants.yaml")
        for r in results[:8]:
            print(f"           pid: {r['pid']}   ({r.get('display_pid', '')})")


if __name__ == "__main__":
    main()
