# Deploying the plant editor on benedict (Ubuntu)

benedict (192.168.0.245) hosts the always-on editor + plant data (source of
truth). The MacBook keeps creating Things tasks but fetches plant data from
benedict. Steps below assume the repo lives at `/home/babe/plantbot`.

## 1. Code + venv on benedict
```sh
git clone <repo> /home/babe/plantbot        # or: git pull
cd /home/babe/plantbot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 2. .env on benedict
Copy `.env.example` to `.env` and fill in:
```env
OPB_CLIENT_ID=...
OPB_CLIENT_SECRET=...
HA_URL=http://127.0.0.1:8123      # HA runs on benedict itself
HA_TOKEN=...
HA_TEMP_ENTITY=...
HA_HUMIDITY_ENTITY=...
LATITUDE=40
```
Seed the plant data once (copy plants.yaml over, or run enrich to build the
species cache).

## 3. Run as a service
```sh
sudo cp deploy/plantbot-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plantbot-server
systemctl status plantbot-server          # should be active; listening on 127.0.0.1:8770
```

## 4. nginx vhost for plant.mom
Additive — does not touch the existing default site.
```sh
sudo cp deploy/nginx-plant.mom.conf /etc/nginx/sites-available/plant.mom
sudo ln -s /etc/nginx/sites-available/plant.mom /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 5. Pi-hole local DNS for plant.mom
Pi-hole runs at 192.168.0.3. Either the admin UI (Local DNS → DNS Records:
`plant.mom` → `192.168.0.245`) or on the Pi-hole host:
```sh
echo "192.168.0.245 plant.mom" | sudo tee -a /etc/pihole/custom.list
sudo pihole restartdns
```
Then `http://plant.mom` serves the editor on the home network.

## 6. Point the MacBook at benedict
In the MacBook's `.env`:
```env
PLANTBOT_SERVER_URL=http://plant.mom
```
`create_watering_tasks.py` and `sync_completed_watering_tasks.py` then use
benedict as the source of truth (via make_stores()). The Mac's launchd schedule
is unchanged.

## Notes
- If `babe` lacks sudo: run the service with `systemctl --user` and access the
  app directly at `http://plant.mom:8770` (skip the nginx step; Pi-hole still
  gives the name). A user service needs `loginctl enable-linger babe` to run
  while logged out.
- Dedicated port 8770 and a name-based vhost keep this isolated from HA (:8123)
  and the existing default site (:80).
