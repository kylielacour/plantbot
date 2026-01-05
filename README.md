# Plantbot 🌱


Plantbot is a personal houseplant care automation that calculates **how much** and **how often** to water each plant based on pot geometry, plant type, soil, and live home climate data. It syncs between **Notion**, **Home Assistant**, and **Things** to create a closed-loop watering system.

## What it does

- Stores plant data in [**Notion**](https://www.notion.com/product/try-ai-meeting-notes)
- Calculates watering **amounts** (measured pours, not soak-until-runoff)
- Calculates watering **intervals** based on environment and growth state
- Pulls live **temperature and humidity** from [**Home Assistant**](https://www.home-assistant.io/)
- Creates watering tasks in [**Things**](https://culturedcode.com/things/)
- Syncs completed tasks back to Notion as “Last Watered”
- Runs automatically on a schedule via [`launchd`](https://www.launchd.info/)

## System Architecture

```text
Home Assistant → Notion (House Conditions)
                     ↓
              Notion (Plant Database)
                     ↓
            Things (Watering Tasks)
                     ↑
        Task Completion → Notion Update
```

## Scripts

`update_house_conditions.py`
- Updates a single “House Conditions” page in Notion with current temperature and humidity from Home Assistant.

`create_watering_tasks.py`
- Reads plant data from Notion, calculates watering needs, and creates tasks in Things.
	
`sync_completed_watering_tasks.py`
- Watches for completed Things tasks and updates the corresponding plant’s “Last Watered” date in Notion.

## Requirements

- macOS (Things 3 + AppleScript)
- Python 3.10+
- Notion account + integration token
- Home Assistant with temperature & humidity sensors
- Things

## Setup (high level)
1. Clone the repo
2. Create a virtual environment
3. Install dependencies
4. Create a .env file with credentials
5. Configure Notion databases
6. Run scripts manually once to verify
7. Schedule scripts with launchd

A .env.example file is included to show required environment variables.

## Notion Templates
Duplicate-ready Notion templates available here:
- [Houseplant Log](https://kyliela.notion.site/2decff8c9db18185a4acf4c6165cf94e?v=2decff8c9db181cabd73000c44df55be&source=copy_link)
- [House Conditions](https://kyliela.notion.site/2decff8c9db180428126f18585025811?v=2decff8c9db181f5aac4000ce324edaa&source=copy_link)

## Environment Variables

```env
NOTION_TOKEN=
NOTION_DB_ID=
HOUSE_PAGE_ID=

HA_URL=
HA_TOKEN=
HA_TEMP_ENTITY=
HA_HUMIDITY_ENTITY=

THINGS_PROJECT_NAME=Plant Care
```

# Watering Model

- Uses internal pot dimensions (soil volume, not outer pot size)
- Targets measured pours (≈5–11% of pot volume)
- Adjusts frequency based on:
  - temperature
  - humidity
  - light
  - growth state
- Designed for real-world indoor plant care, including pots without drainage

## Notes

- This system intentionally avoids “water until runoff” assumptions.
- Soil and pot materials are categorized by behavior, not ingredients.
- Seasonal changes are handled automatically via environment data.
