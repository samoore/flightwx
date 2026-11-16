import csv, io, os, time, requests
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request

app = Flask(__name__, static_folder="static", static_url_path="/static")
_cache = {}
LEVELS = [850, 700, 500, 400, 300, 250, 200, 150]  # hPa, ~FL050 .. FL450
# Higher-res regional models tried first inside their box (S, W, N, E); else Open-Meteo best_match.
# Edit freely; model names per Open-Meteo docs (verify). Missing upper-level data falls back automatically.
REGIONS = [("ukmo_uk_deterministic_2km", (49, -11, 61, 2)),
           ("icon_d2", (43, -4, 58, 20)),
           ("gfs_hrrr", (21, -134, 53, -60))]
DEFAULT_MODEL = os.environ.get("FLIGHTWX_MODEL", "")  # e.g. ecmwf_ifs025 outside REGIONS
CLOUD_LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150]  # for cloud base/top estimate
EMPTY = {"lv": {l: {"ws": None, "wd": None, "t": None, "w": None} for l in LEVELS},
         "cape": None, "lpi": None, "precip": None, "cloud": None}


def isa_ft(p):
    return 145366 * (1 - (p / 1013.25) ** 0.190284)  # hPa -> ft, standard atmosphere


def fetch(url, params=None, json=None, ttl=60, headers=None):
    """GET (POST if json) with a tiny in-memory cache."""
    key = (url, str(params), str(json))
    if key in _cache and time.time() - _cache[key][0] < ttl:
        return _cache[key][1]
    r = requests.request("POST" if json else "GET", url, params=params, json=json,
                         timeout=15, headers={"User-Agent": "flightwx/0.3", **(headers or {})})
    r.raise_for_status()
    _cache[key] = (time.time(), r.json())
    return _cache[key][1]


@app.route("/")
def index():
    return app.send_static_file("index.html")


def route_for(cs):
    """Origin/destination for a flight number (ICAO or IATA, e.g. ANZ1 / NZ1) from adsbdb:
    open, keyless, works before departure."""
    try:
        fr = fetch(f"https://api.adsbdb.com/v0/callsign/{cs}", ttl=3600)["response"]["flightroute"]
        o, d = fr["origin"], fr["destination"]
        return dict(route=[[o["latitude"], o["longitude"]], [d["latitude"], d["longitude"]]],
                    codes=[o["icao_code"], d["icao_code"]], icao=fr.get("callsign_icao"), iata=fr.get("callsign_iata"))
    except Exception:
        return {}


def next_departure(iata):
    """Next departure (UTC) from AeroDataBox (RapidAPI) if AERODATABOX_KEY is set; else None.
    There is no open, keyless schedule source. Check field names / plan limits against their docs."""
    key = os.environ.get("AERODATABOX_KEY")
    if not (key and iata):
        return None
    now, best = datetime.now(timezone.utc), None
    for day in (0, 1):  # today and tomorrow (UTC dates; local-date edge cases possible)
        d = (now + timedelta(days=day)).strftime("%Y-%m-%d")
        try:
            fl = fetch(f"https://aerodatabox.p.rapidapi.com/flights/number/{iata}/{d}", ttl=3600,
                       headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com"})
            for x in fl if isinstance(fl, list) else []:
                dp = x["departure"]
                dt = datetime.fromisoformat((dp.get("revisedTime") or dp["scheduledTime"])["utc"].replace("Z", "+00:00"))
                if dt > now - timedelta(hours=1) and (best is None or dt < best):
                    best = dt
        except Exception:
            pass
    return best


@app.route("/api/flight/<cs>")
def flight(cs):
    """Flight number -> route + (live position | next departure time)."""
    q = cs.upper().replace(" ", "")
    out = route_for(q)
    ac = None
    for c in dict.fromkeys(filter(None, [out.get("icao"), q])):  # ADS-B needs ICAO callsign (ANZ1, not NZ1)
        try:  # adsb.lol: open community ADS-B network, no key
            a = (fetch(f"https://api.adsb.lol/v2/callsign/{c}", ttl=10).get("ac") or [None])[0]
        except Exception:
            a = None
        if a and "lat" in a:
            ac = a
            break
    out["live"] = bool(ac)
    if ac:
        alt = ac.get("alt_baro")
        out.update(lat=ac["lat"], lon=ac["lon"], track=ac.get("track", 0), gs=ac.get("gs", 450),
                   alt_ft=alt if isinstance(alt, (int, float)) else 0)
        if "route" not in out:  # adsbdb had no route: try adsb.lol (needs a position)
            try:
                rs = fetch("https://api.adsb.lol/api/0/routeset",
                           json={"planes": [{"callsign": q, "lat": ac["lat"], "lng": ac["lon"]}]}, ttl=600)
                ap = rs[0]["_airports"]
                out.update(route=[[x["lat"], x["lon"]] for x in ap], codes=[x.get("icao") for x in ap])
            except Exception:
                pass
    elif "route" in out:
        dt = next_departure(out.get("iata"))
        if dt:
            out.update(dep_ms=int(dt.timestamp() * 1000), dep_src="next scheduled departure")
        else:
            top = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            out.update(dep_ms=int(top.timestamp() * 1000), dep_src="ASSUMED next hour (no schedule key; enter a time to override)")
    else:
        return jsonify(error=f"{q}: not airborne and route unknown. Check the flight number or enter origin-dest."), 404
    return jsonify(out)


AIRPORTS = {}


@app.route("/api/airports")
def airports():
    """ids=NZAA,YSSY -> [[lat, lon], ...] from OurAirports (downloaded once per run)."""
    if not AIRPORTS:
        txt = requests.get("https://davidmegginson.github.io/ourairports-data/airports.csv", timeout=60).text
        for r in csv.DictReader(io.StringIO(txt)):
            AIRPORTS[r["ident"]] = [float(r["latitude_deg"]), float(r["longitude_deg"])]
    ids = request.args["ids"].upper().split(",")
    if any(i not in AIRPORTS for i in ids):
        return jsonify(error="unknown ICAO code"), 404
    return jsonify([AIRPORTS[i] for i in ids])


def pick_model(lat, lon):
    for m, (s, w, n, e) in REGIONS:
        if s <= lat <= n and w <= lon <= e:
            return m
    return DEFAULT_MODEL


def om(pts, model):
    """Open-Meteo for [lat, lon, "YYYY-MM-DDTHH:00" UTC] points. Winds/temp/vertical velocity on LEVELS,
    cloud cover on CLOUD_LEVELS (-> cloud base/top), plus CAPE, lightning potential, precipitation."""
    hourly = ",".join(f"{v}_{l}hPa" for l in LEVELS
                      for v in ("wind_speed", "wind_direction", "temperature", "vertical_velocity"))
    hourly += "," + ",".join(f"cloud_cover_{l}hPa" for l in CLOUD_LEVELS) + ",cape,lightning_potential,precipitation"
    params = dict(latitude=",".join(p[0] for p in pts), longitude=",".join(p[1] for p in pts),
                  hourly=hourly, wind_speed_unit="kn", timezone="GMT", past_days=1, forecast_days=3)
    if model:
        params["models"] = model
    d = fetch("https://api.open-meteo.com/v1/forecast", params, ttl=600)
    d = d if isinstance(d, list) else [d]
    out = []
    for loc, p in zip(d, pts):
        times = loc["hourly"]["time"]
        i = times.index(p[2]) if p[2] in times else None  # None = outside yesterday..+2 days window
        g = lambda k: (loc["hourly"].get(k) or [None] * len(times))[i] if i is not None else None
        ms = lambda v: None if v is None else round(v * 0.5144, 2)  # kn -> m/s (vertical velocity follows wind unit; verify)
        known = [(l, g(f"cloud_cover_{l}hPa")) for l in CLOUD_LEVELS]
        known = [(l, c) for l, c in known if c is not None]
        thick = [l for l, c in known if c >= 50]
        cloud = None if not known else ({"base": isa_ft(max(thick)), "top": isa_ft(min(thick))} if thick else "clear")
        out.append({"lv": {l: {"ws": g(f"wind_speed_{l}hPa"), "wd": g(f"wind_direction_{l}hPa"),
                               "t": g(f"temperature_{l}hPa"), "w": ms(g(f"vertical_velocity_{l}hPa"))} for l in LEVELS},
                    "cape": g("cape"), "lpi": g("lightning_potential"), "precip": g("precipitation"), "cloud": cloud})
    return out


def fill(out, idx, pts, model):
    try:
        for i, r in zip(idx, om([pts[i] for i in idx], model)):
            out[i] = r
    except Exception:
        pass


@app.route("/api/wx")
def wx():
    """pts='lat,lon,YYYY-MM-DDTHH:00(UTC);...'  model='' (auto: regional then best_match) or an Open-Meteo model name"""
    pts = [p.split(",") for p in request.args["pts"].split(";")]
    forced, out, groups = request.args.get("model"), [None] * len(pts), {}
    for i, p in enumerate(pts):
        groups.setdefault(forced or pick_model(float(p[0]), float(p[1])), []).append(i)
    for m, idx in groups.items():
        fill(out, idx, pts, m)
    if not forced:  # regional model failed or lacks upper levels -> best_match
        bad = [i for i, r in enumerate(out) if r is None or all(v["ws"] is None for v in r["lv"].values())]
        if bad:
            fill(out, bad, pts, "")
    return jsonify([r or EMPTY for r in out])


@app.route("/api/obs")
def obs():
    """METARs (NOAA AWC) + SIGMETs touching bbox=S,W,N,E."""
    s, w, n, e = (float(x) for x in request.args["bbox"].split(","))
    wrap = lambda x: (x + 180) % 360 - 180
    w, e = wrap(w), wrap(e)
    boxes = [(s, w, n, e)] if w <= e else [(s, w, n, 180), (s, -180, n, e)]  # split at dateline
    inside = lambda la, lo: any(a <= la <= c and b <= lo <= d for a, b, c, d in boxes)
    metar = []
    for a, b, c, d in boxes:
        try:
            metar += fetch("https://aviationweather.gov/api/data/metar",
                           {"bbox": f"{a},{b},{c},{d}", "format": "json"}, ttl=300) or []
        except Exception:
            pass
    sig = []
    for src in ("airsigmet", "isigmet"):
        try:
            for x in fetch(f"https://aviationweather.gov/api/data/{src}", {"format": "json"}, ttl=300):
                c = [(p["lat"], p["lon"]) for p in x.get("coords") or []]
                if len(c) > 2 and any(inside(la, lo) for la, lo in c):
                    sig.append(dict(hazard=x.get("hazard"), sev=x.get("severity"),
                                    raw=(x.get("rawAirSigmet") or x.get("rawSigmet") or "")[:300],
                                    coords=[[lo, la] for la, lo in c] + [[c[0][1], c[0][0]]]))
        except Exception:
            pass
    return jsonify(metar=[dict(id=m.get("icaoId"), lat=m["lat"], lon=m["lon"], t=m.get("temp"),
                               wd=m.get("wdir"), ws=m.get("wspd"), cat=m.get("fltCat"), raw=m.get("rawOb"))
                          for m in metar if "lat" in m][:150], sigmet=sig)


@app.errorhandler(Exception)
def on_error(e):  # upstream API down / offline -> JSON the page can show, not an HTML 500
    code = getattr(e, "code", 502)
    return jsonify(error=f"{type(e).__name__}: {str(e)[:200]}"), code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    page = os.path.join(app.static_folder, "index.html")
    if not os.path.exists(page):
        raise SystemExit(f"Missing {page}\nindex.html must sit in a 'static' folder next to app.py.")
    print(f"\n  Open  http://127.0.0.1:{port}/?cs=ANZ1\n")
    app.run(host="127.0.0.1", port=port)  # laptop only; use 0.0.0.0 to share on your network
