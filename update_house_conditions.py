#!/usr/bin/env python3
import os
import requests
import datetime as dt

# ===== Env =====
HA_URL = os.environ["HA_URL"]
HA_TOKEN = os.environ["HA_TOKEN"]
TEMP_ENTITY = os.environ["HA_TEMP_ENTITY"]
HUM_ENTITY = os.environ["HA_HUMIDITY_ENTITY"]

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
HOUSE_PAGE_ID = os.environ["HOUSE_PAGE_ID"]  # single row ID

NOTION_VERSION = "2022-06-28"

# ===== Helpers =====
def ha_headers():
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def get_state(entity_id):
    r = requests.get(f"{HA_URL}/api/states/{entity_id}", headers=ha_headers(), timeout=10)
    r.raise_for_status()
    return r.json()["state"]

# ===== Main =====
def main():
    temp = float(get_state(TEMP_ENTITY))
    humidity = float(get_state(HUM_ENTITY))

    payload = {
        "properties": {
            "Temperature (F)": {"number": temp},
            "Humidity (%)": {"number": humidity},
            "Updated At": {
                "date": {"start": dt.datetime.now().isoformat()}
            },
        }
    }

    url = f"https://api.notion.com/v1/pages/{HOUSE_PAGE_ID}"
    r = requests.patch(url, headers=notion_headers(), json=payload, timeout=10)
    r.raise_for_status()

    print(f"Updated house conditions: {temp}°F, {humidity}%")

if __name__ == "__main__":
    main()
