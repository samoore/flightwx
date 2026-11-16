# flightwx
Live flight position (adsb.lol) + weather along route (Open-Meteo, NOAA AWC METAR/SIGMET, RainViewer radar)
on a MapLibre globe, with a wind cross-section along the route.

## Environment (laptop)
Python >= 3.10 and a browser. Nothing else (no Node, no build step, no API keys).

    # option A: venv                              # option B: conda/mamba
    python -m venv .venv                          conda env create -f environment.yml
    source .venv/bin/activate                     conda activate flightwx
    pip install -r requirements.txt

    python app.py        # then open http://127.0.0.1:8000/?cs=ANZ1

Python packages: `flask`, `requests`. (`gunicorn` only if you later host it on a server.)
Browser libs load from the unpkg CDN (MapLibre GL JS 5.x). For offline/captive-portal use, download
`maplibre-gl.js` + `.css` into `static/vendor/` and point the two tags at them.

Outbound HTTPS needed: api.adsb.lol, api.open-meteo.com, aviationweather.gov, api.rainviewer.com,
tile.openstreetmap.org, unpkg.com. Open-Meteo is free for non-commercial use with daily call limits;
responses are cached 10 min server-side, and the page refreshes every 2 min.

## Flight lookup
Enter just a flight number (`NZ1` or `ANZ1`). Route comes from adsbdb (open, no key) so it works before
departure. If airborne, position is live from adsb.lol and departure is estimated from distance flown.
If not airborne, departure is the next scheduled one when `AERODATABOX_KEY` (RapidAPI) is set, otherwise
ASSUMED to be the next full hour (shown in the page header). No open, keyless schedule source exists.
The origin-dest and departure boxes are optional overrides. Adsbdb gives origin/destination only, so
multi-stop flight numbers show the full origin-to-destination great circle.

## Reading the map
- **Coloured dots** are airports with a current METAR (NOAA Aviation Weather Center) near the route, coloured by
  flight category: green VFR, blue MVFR, red IFR, magenta LIFR, grey unknown. Hover a dot for the raw METAR.
- **Orange areas** are SIGMETs: forecast hazard advisories for aviation (thunderstorms, severe turbulence, icing,
  volcanic ash, tropical cyclones). They are **not** no-fly zones or airspace closures. Click an area for its text.
- **Rain echoes** are radar (RainViewer). **Small arrows** show forecast wind at the aircraft's level and point downwind.
- **Plane icon:** live position if airborne (rotated to its track). If the flight is not on ADS-B it is shown at the
  origin (not yet departed), at the destination (the modelled arrival time has passed), or interpolated by time.
  Black line = flown part, red dashed = remaining. Landed flights drop off ADS-B, so "arrived" is inferred from times.

## Timing model
Each point's time = distance (great circle x 1.04 for a flown route) / (480 kt TAS + forecast along-track wind).
Winds are fetched, times recomputed, and the weather re-fetched if the hours moved. Climb/descent, ATC routing and
real schedules are not modelled, so expect a few tens of minutes of error. For a live flight times are anchored
on the current position/time, so past times are model estimates too. (An earlier version used one constant ground
speed, which inflated long flights.)

## Table fields
Wind, temperature and vertical velocity at the aircraft's level, plus rain, cloud base/top (from pressure-level
cloud cover >= 50%, coarse), CAPE and lightning potential. Altitude is live (ADS-B) only for the aircraft's
current row; other rows use an assumed climb/cruise/descent profile. Vertical velocity and cloud levels are only
available from some models (ECMWF IFS 0.25, ICON, ...); unavailable fields show a dash. Each refresh requests
~45 variables x ~12 points from Open-Meteo (cached 10 min), which counts as several API calls against its free limit.

## Oceania notes
- Callsigns are reused daily and ADS-B only sees airborne aircraft. For a flight not yet departed, enter
  `NZAA-YSSY` (ICAO origin-dest) and a departure time; the route is then a great circle at 450 kt, FL350.
- Departure time is entered in your browser's local time and shown as UTC (Z). For a live flight, leave it
  blank: departure is estimated from distance flown. Weather is picked for the hour each point is reached.
- Set `FLIGHTWX_MODEL=ecmwf_ifs025` (or `gfs_global`) to change the default global model.

## Git
    git init && git add . && git commit -m "flightwx v0.2"

## Remote access later
`tailscale up` on the laptop, run with host 0.0.0.0, open http://<laptop-name>:8000 from your phone.

## Notes / to verify
- Endpoint and field names (adsb.lol routeset, AWC JSON, RainViewer) are unverified; adjust if they changed.
- Regional model boxes live in `REGIONS` in app.py; auto mode falls back to best_match if a model lacks upper levels.
- METAR/SIGMET overlay is skipped for routes spanning > 180° of longitude (bbox query limitation).
