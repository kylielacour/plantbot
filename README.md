# Plantbot 🌱

Plantbot is a personal houseplant-care automation. It calculates **how much** and **how
often** to water each plant from pot geometry, soil, species, and live home climate, then
creates the watering tasks in [**Things**](https://culturedcode.com/things/) and records
completions back to local state.

Plants are defined in a local `plants.yaml` file. Per-species care thresholds come from
[**Open Plantbook**](https://open.plantbook.io) (free). Climate comes from your existing
[**Home Assistant**](https://www.home-assistant.io/) thermostat when available, with static
indoor defaults as a fallback — so it works with **no sensors at all**.

## What it does

- Stores plant data in a local, version-controlled **`plants.yaml`**
- Pulls per-species setpoints (soil-moisture band, light/temp/humidity comfort) from **Open
  Plantbook**, cached locally
- Calculates watering **amount** (a measured pour) and **interval** using a physically-grounded
  model (see below)
- Uses live **temperature + humidity** from **Home Assistant** if configured, else static defaults
- Creates watering tasks in **Things**, with an explanation of *why* in the notes
- Syncs completed tasks back into `state/watering_state.json` as each plant's `last_watered`
- Runs on a schedule via [`launchd`](https://www.launchd.info/)

## Architecture

```text
plants.yaml ──┐
Open Plantbook ┼─► watering_model ─► Things (watering tasks)
Home Assistant ┘                              │
        (temp/humidity, optional)             ▼
state/watering_state.json ◄── Things Logbook (completed) 
```

## The watering model

The calculation is pure Python (`watering_model.py`) and unit-tested. Two quantities:

- **Amount** = `soil_volume × available-water-capacity(soil) × allowed-depletion(species)`,
  capped to a measured pour (≤20% of soil volume, reduced further for pots without drainage).
  Driven by pot geometry + Open Plantbook — **no sensor required**.
- **Interval** = `amount ÷ daily-water-loss`, where daily loss is an evapotranspiration estimate
  scaled by:
  - **VPD** (vapour-pressure deficit — combines temperature + humidity, the physically correct
    driver of transpiration)
  - **light** (per-plant exposure category — measure your spot once with a phone lux app)
  - **season / day-length** (from your latitude)
  - **growth state** (active / dormant / `auto`, where `auto` detects winter dormancy)
  - **water-use** (succulent → low, most foliage → medium, fern/calathea → high)

## Sensors (optional)

It works today with **zero plant sensors** (your thermostat covers climate; light is a one-time
measurement). If you want to close the loop later, the only sensor worth adding is **per-plant
soil moisture** — e.g. a Xiaomi *Flower Care* / MiFlora, or a DIY ESPHome **capacitive** probe
(never resistive — they corrode). Start with your thirstiest / hardest-to-read plants.

## Setup

1. Clone the repo, create a venv, `pip install -r requirements.txt`
2. `cp .env.example .env` and fill in credentials (see below)
3. `cp plants.example.yaml plants.yaml` and describe your plants
4. `python enrich_plants.py` to resolve Open Plantbook species ids and warm the cache
5. `python create_watering_tasks.py --dry-run` to preview the schedule
6. Schedule `run_watering_cycle.sh` with launchd

Migrating from the old Notion setup? `python migrate_notion_to_yaml.py > plants.yaml` generates
a skeleton and carries over your `Last Watered` dates.

## Environment variables

```env
# Open Plantbook (recommended) — generate under "API keys" at open.plantbook.io
OPB_CLIENT_ID=
OPB_CLIENT_SECRET=

# Home Assistant (optional) — live temp/humidity; falls back to static defaults
HA_URL=
HA_TOKEN=
HA_TEMP_ENTITY=
HA_HUMIDITY_ENTITY=
HA_LUX_ENTITY=      # optional
HA_TEMP_UNIT=F      # F or C

LATITUDE=40         # for day-length / season / dormancy

THINGS_PROJECT_NAME=Plant Care

# Notion — only for the one-time migrate_notion_to_yaml.py import
NOTION_TOKEN=
NOTION_DATABASE_ID=
```

## Scripts

- `create_watering_tasks.py` — compute needs and create Things tasks (`--dry-run` to preview)
- `sync_completed_watering_tasks.py` — record completed waterings into local state
- `enrich_plants.py` — resolve/refresh Open Plantbook species data
- `migrate_notion_to_yaml.py` — one-time Notion → `plants.yaml` export
- `run_watering_cycle.sh` — runs sync, then create (for launchd)

## Testing

```sh
./venv/bin/python -m pytest tests/ -q
```

## Requirements

- macOS (Things 3 + AppleScript)
- Python 3.10+
- Things 3
- Optional: Home Assistant with temp/humidity sensors; Open Plantbook account

## Notes

- Avoids "water until runoff": pours are measured and capped.
- Open Plantbook values are crowd-sourced defaults — override any of them per plant under
  `overrides:` in `plants.yaml`.
- `plants.yaml` and `state/` are git-ignored; `plants.example.yaml` is committed.
