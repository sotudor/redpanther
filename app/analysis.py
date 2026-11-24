"""Transformă fluxul de telemetrie în drumuri, încărcări, perioade de parcare și statistici."""
import asyncio
import json
import math
import time
from datetime import date, datetime, timedelta

import httpx

MILES = 1.609344
DRIVING = {"ShiftStateD", "ShiftStateR", "ShiftStateN"}
PARKED = {"ShiftStateP"}
CHARGING = {"DetailedChargeStateCharging", "DetailedChargeStateStarting"}
# O încărcare = de la cuplarea cablului până la decuplare, ca în aplicația Tesla,
# chiar dacă între timp încărcarea se oprește și pornește de mai multe ori.
PLUGGED = {"DetailedChargeStateNoPower", "DetailedChargeStateStarting", "DetailedChargeStateCharging",
           "DetailedChargeStateComplete", "DetailedChargeStateStopped"}
UNPLUGGED = {"DetailedChargeStateDisconnected"}
CHARGES_VERSION = "2"   # la schimbarea regulii, încărcările vechi se recalculează o dată
SENTRY_OFF = {"SentryModeStateOff", "SentryModeStateUnknown"}
STALE_SECONDS = 30 * 60
DEFAULT_KWH = 75.0
GEOCODE_UA = "RedPanther/1.0 (aplicatie personala Tesla)"


def init_db(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS drives (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, start_ts REAL, end_ts REAL,
            start_odo REAL, end_odo REAL, start_energy REAL, end_energy REAL,
            start_soc REAL, end_soc REAL, start_range REAL, end_range REAL,
            start_lat REAL, start_lon REAL, end_lat REAL, end_lon REAL,
            max_speed REAL, out_temp REAL, UNIQUE (vin, start_ts)
        );
        CREATE TABLE IF NOT EXISTS charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, start_ts REAL, end_ts REAL,
            start_soc REAL, end_soc REAL, start_energy REAL, end_energy REAL,
            energy_in REAL, max_power REAL, dc INTEGER, lat REAL, lon REAL,
            manual_cost REAL, manual_currency TEXT, UNIQUE (vin, start_ts)
        );
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, lat REAL, lon REAL, radius_m REAL
        );
        CREATE TABLE IF NOT EXISTS geocache (key TEXT PRIMARY KEY, label TEXT);
        CREATE TABLE IF NOT EXISTS sc_sessions (
            session_id TEXT PRIMARY KEY, vin TEXT, start_ts REAL, end_ts REAL,
            site TEXT, cost REAL, currency TEXT
        );
        CREATE TABLE IF NOT EXISTS capacity_log (vin TEXT, day TEXT, kwh REAL, PRIMARY KEY (vin, day));
        CREATE TABLE IF NOT EXISTS vehicle_settings (
            vin TEXT PRIMARY KEY, nominal_kwh REAL, has_photo INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS analysis_meta (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(charges)")}
    if "charge_seconds" not in cols:
        con.execute("ALTER TABLE charges ADD COLUMN charge_seconds REAL")


# ---------- citirea seriilor ----------

def value_at(con, vin, field, ts, prefer_after=False):
    q_before = "SELECT value FROM telemetry_log WHERE vin=? AND field=? AND ts<=? ORDER BY ts DESC LIMIT 1"
    q_after = "SELECT value FROM telemetry_log WHERE vin=? AND field=? AND ts>=? ORDER BY ts ASC LIMIT 1"
    order = (q_after, q_before) if prefer_after else (q_before, q_after)
    for q in order:
        r = con.execute(q, (vin, field, ts)).fetchone()
        if r is not None:
            return r[0]
    return None


def loc_at(con, vin, ts, prefer_after=False):
    q_before = "SELECT lat, lon FROM telemetry_loc WHERE vin=? AND ts<=? ORDER BY ts DESC LIMIT 1"
    q_after = "SELECT lat, lon FROM telemetry_loc WHERE vin=? AND ts>=? ORDER BY ts ASC LIMIT 1"
    for q in ((q_after, q_before) if prefer_after else (q_before, q_after)):
        r = con.execute(q, (vin, ts)).fetchone()
        if r is not None:
            return r[0], r[1]
    return None, None


def series(con, vin, field, t0, t1):
    rows = con.execute(
        "SELECT ts, value FROM telemetry_log WHERE vin=? AND field=? AND ts BETWEEN ? AND ? ORDER BY ts",
        (vin, field, t0, t1),
    ).fetchall()
    return [[r[0], r[1]] for r in rows]


def agg(con, fn, vin, field, t0, t1):
    r = con.execute(
        f"SELECT {fn}(value) FROM telemetry_log WHERE vin=? AND field=? AND ts BETWEEN ? AND ?",
        (vin, field, t0, t1),
    ).fetchone()
    return r[0] if r else None


def last_data_ts(con, vin):
    r = con.execute("SELECT MAX(ts) FROM telemetry_log WHERE vin=?", (vin,)).fetchone()
    return r[0] if r and r[0] else None


def counter_increments(con, vin, field, t0, t1):
    """Creșterile unui contor de energie (ex. ACChargingEnergyIn) în [t0, t1], ca listă (ts, kWh).
    Contorul poate porni de la zero la o nouă sesiune; o scădere înseamnă că s-a resetat."""
    r = con.execute("SELECT value FROM telemetry_log WHERE vin=? AND field=? AND ts<? ORDER BY ts DESC LIMIT 1",
                    (vin, field, t0)).fetchone()
    prev = r[0] if r else None
    out = []
    for ts, v in series(con, vin, field, t0, t1):
        if prev is not None:
            d = v - prev if v >= prev else v
            if d > 0:
                out.append((ts, d))
        prev = v
    return out


def energy_increments(con, vin, t0, t1):
    """Energia luată de la încărcător în [t0, t1], ca (ts, kWh). Dacă mașina nu a trimis
    contoarele de energie, folosește creșterile energiei din baterie."""
    ac = counter_increments(con, vin, "ACChargingEnergyIn", t0, t1)
    dc = counter_increments(con, vin, "DCChargingEnergyIn", t0, t1)
    best = max((ac, dc), key=lambda inc: sum(d for _, d in inc))
    if best:
        return best
    pts = series(con, vin, "EnergyRemaining", t0, t1)
    return [(b[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]) if b[1] > a[1]]


def state_seconds(con, vin, field, states, t0, t1):
    """Câte secunde din [t0, t1] a stat un câmp de tip enum într-una din stările date."""
    r = con.execute("SELECT value FROM telemetry_events WHERE vin=? AND field=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                    (vin, field, t0)).fetchone()
    cur_in, cur_ts, total = bool(r and r[0] in states), t0, 0.0
    for ts, v in con.execute("SELECT ts, value FROM telemetry_events WHERE vin=? AND field=? AND ts>? AND ts<=? "
                             "ORDER BY ts", (vin, field, t0, t1)):
        if cur_in:
            total += ts - cur_ts
        cur_in, cur_ts = v in states, ts
    if cur_in:
        total += t1 - cur_ts
    return total


def segments(con, vin, field, start_set, end_set, since, close_stale=True):
    """Intervale [start, end) în care un câmp de tip enum stă în `start_set`.
    Cu `close_stale`, un interval fără date de peste 30 de minute se consideră încheiat."""
    prev = con.execute(
        "SELECT value FROM telemetry_events WHERE vin=? AND field=? AND ts<? ORDER BY ts DESC LIMIT 1",
        (vin, field, since),
    ).fetchone()
    cur = since if prev and prev[0] in start_set else None
    out = []
    for ts, v in con.execute(
        "SELECT ts, value FROM telemetry_events WHERE vin=? AND field=? AND ts>=? ORDER BY ts",
        (vin, field, since),
    ):
        if v in start_set and cur is None:
            cur = ts
        elif v in end_set and cur is not None:
            out.append((cur, ts))
            cur = None
    if cur is not None:
        last = last_data_ts(con, vin) or cur
        if close_stale and time.time() - last > STALE_SECONDS:
            out.append((cur, max(last, cur)))
        else:
            out.append((cur, None))
    return out


# ---------- analiza ----------

def analyze(con, vin, days=2):
    now = time.time()
    open_min = con.execute(
        "SELECT MIN(m) FROM (SELECT MIN(start_ts) m FROM drives WHERE vin=? AND end_ts IS NULL "
        "UNION ALL SELECT MIN(start_ts) FROM charges WHERE vin=? AND end_ts IS NULL)",
        (vin, vin),
    ).fetchone()[0]
    since = now - days * 86400
    if open_min:
        since = min(since, open_min - 1)

    # drumuri
    produced = set()
    for t0, t1 in segments(con, vin, "Gear", DRIVING, PARKED, since):
        te = t1 or now
        so, eo = value_at(con, vin, "Odometer", t0), value_at(con, vin, "Odometer", te)
        dist = (eo - so) * MILES if so is not None and eo is not None else 0
        if t1 is not None and dist < 0.1 and te - t0 < 120:
            continue
        sla, slo = loc_at(con, vin, t0, prefer_after=True)
        ela, elo = loc_at(con, vin, te)
        row = (
            vin, t0, t1, so, eo,
            value_at(con, vin, "EnergyRemaining", t0), value_at(con, vin, "EnergyRemaining", te),
            value_at(con, vin, "Soc", t0), value_at(con, vin, "Soc", te),
            value_at(con, vin, "RatedRange", t0), value_at(con, vin, "RatedRange", te),
            sla, slo, ela, elo,
            agg(con, "MAX", vin, "VehicleSpeed", t0, te),
            agg(con, "AVG", vin, "OutsideTemp", t0, te) or value_at(con, vin, "OutsideTemp", t0),
        )
        con.execute(
            """INSERT INTO drives (vin, start_ts, end_ts, start_odo, end_odo, start_energy, end_energy,
               start_soc, end_soc, start_range, end_range, start_lat, start_lon, end_lat, end_lon,
               max_speed, out_temp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(vin, start_ts) DO UPDATE SET end_ts=excluded.end_ts, start_odo=excluded.start_odo,
               end_odo=excluded.end_odo, start_energy=excluded.start_energy, end_energy=excluded.end_energy,
               start_soc=excluded.start_soc, end_soc=excluded.end_soc, start_range=excluded.start_range,
               end_range=excluded.end_range, start_lat=excluded.start_lat, start_lon=excluded.start_lon,
               end_lat=excluded.end_lat, end_lon=excluded.end_lon, max_speed=excluded.max_speed,
               out_temp=excluded.out_temp""",
            row,
        )
        produced.add(t0)
    for r in con.execute("SELECT id, start_ts FROM drives WHERE vin=? AND start_ts>=?", (vin, since)).fetchall():
        if r[1] not in produced:
            con.execute("DELETE FROM drives WHERE id=?", (r[0],))

    analyze_charges(con, vin, since, now)

    # capacitatea estimată a bateriei (pentru „battery health”)
    es = series(con, vin, "EnergyRemaining", now - 86400, now)
    ss = series(con, vin, "Soc", now - 86400, now)
    pairs = []
    j = 0
    for ts, e in es:
        while j + 1 < len(ss) and abs(ss[j + 1][0] - ts) <= abs(ss[j][0] - ts):
            j += 1
        if ss and abs(ss[j][0] - ts) < 300:
            pairs.append((e, ss[j][1]))
    est = sorted(e / (s / 100) for e, s in pairs if e and s and s >= 40)
    if est:
        day = date.today().isoformat()
        con.execute(
            "INSERT INTO capacity_log VALUES (?,?,?) ON CONFLICT(vin, day) DO UPDATE SET kwh=excluded.kwh",
            (vin, day, est[len(est) // 2]),
        )


def analyze_charges(con, vin, since, now):
    """Încărcările = intervalele în care cablul e cuplat. Un interval cu cablul încă în priză
    rămâne „în curs” chiar dacă mașina doarme: la decuplare se trezește și trimite starea."""
    # Dacă `since` cade în mijlocul unei încărcări deja salvate, o recalculăm de la început.
    r = con.execute("SELECT MIN(start_ts) FROM charges WHERE vin=? AND (end_ts IS NULL OR end_ts>=?)",
                    (vin, since)).fetchone()
    if r[0] is not None:
        since = min(since, r[0] - 1)
    produced = set()
    for t0, t1 in segments(con, vin, "DetailedChargeState", PLUGGED, UNPLUGGED, since, close_stale=False):
        te = t1 or now
        se, ee = value_at(con, vin, "EnergyRemaining", t0), value_at(con, vin, "EnergyRemaining", te)
        e_in = sum(d for _, d in energy_increments(con, vin, t0, te))
        gain = (ee - se) if se is not None and ee is not None else 0
        if t1 is not None and gain < 0.2 and e_in < 0.2:
            continue   # cuplată, dar fără energie adăugată (ex. încărcare programată care n-a pornit)
        dc_power = agg(con, "MAX", vin, "DCChargingPower", t0, te) or 0
        ac_power = agg(con, "MAX", vin, "ACChargingPower", t0, te) or 0
        fast = agg(con, "MAX", vin, "FastChargerPresent", t0, te) or 0
        la, lo = loc_at(con, vin, t0)
        con.execute(
            """INSERT INTO charges (vin, start_ts, end_ts, start_soc, end_soc, start_energy, end_energy,
               energy_in, max_power, dc, lat, lon, charge_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(vin, start_ts) DO UPDATE SET end_ts=excluded.end_ts, start_soc=excluded.start_soc,
               end_soc=excluded.end_soc, start_energy=excluded.start_energy, end_energy=excluded.end_energy,
               energy_in=excluded.energy_in, max_power=excluded.max_power, dc=excluded.dc,
               lat=excluded.lat, lon=excluded.lon, charge_seconds=excluded.charge_seconds""",
            (vin, t0, t1, value_at(con, vin, "Soc", t0), value_at(con, vin, "Soc", te), se, ee,
             e_in or None, max(dc_power, ac_power) or None, 1 if (dc_power > 1 or fast >= 1) else 0, la, lo,
             state_seconds(con, vin, "DetailedChargeState", CHARGING, t0, te)),
        )
        produced.add(t0)
    stale = [r for r in con.execute(
        "SELECT id, start_ts, manual_cost, manual_currency FROM charges WHERE vin=? AND start_ts>=?", (vin, since))
        if r[1] not in produced]
    for r in stale:
        con.execute("DELETE FROM charges WHERE id=?", (r[0],))
    for r in stale:
        if r[2] is not None:
            # Costul introdus manual pe o bucată veche trece pe încărcarea care o cuprinde acum.
            target = con.execute(
                "SELECT id FROM charges WHERE vin=? AND start_ts<=? AND (end_ts IS NULL OR end_ts>=?) "
                "AND manual_cost IS NULL ORDER BY start_ts DESC LIMIT 1", (vin, r[1], r[1])).fetchone()
            if target:
                con.execute("UPDATE charges SET manual_cost=?, manual_currency=? WHERE id=?", (r[2], r[3], target[0]))


def charged_by_day(con, vin, days, tz):
    """Energia luată de la încărcător în fiecare zi din `days`, după momentul în care a intrat,
    deci o încărcare peste noapte se împarte corect între cele două zile."""
    t0, _ = day_bounds(days[0], days[0], tz)
    _, t1 = day_bounds(days[-1], days[-1], tz)
    per = {k: 0.0 for k in days}
    for c in con.execute("SELECT start_ts, end_ts FROM charges WHERE vin=? AND start_ts<? AND "
                         "(end_ts IS NULL OR end_ts>?)", (vin, t1, t0)).fetchall():
        a, b = max(c[0], t0), min(c[1] or time.time(), t1)
        for ts, kwh in energy_increments(con, vin, a, b):
            k = datetime.fromtimestamp(ts, tz).date().isoformat()
            if k in per:
                per[k] += kwh
    return [{"day": k, "kwh": round(per[k], 2)} for k in days]


def days_between(day_from: str, day_to: str):
    out, d = [], datetime.fromisoformat(day_from).date()
    while d <= datetime.fromisoformat(day_to).date():
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def analyze_all(db, days=2):
    with db() as con:
        vins = [r[0] for r in con.execute("SELECT DISTINCT vin FROM telemetry_events")]
        redo = (con.execute("SELECT v FROM analysis_meta WHERE k='charges_version'").fetchone() or [None])[0] != CHARGES_VERSION
        for vin in vins:
            if redo:
                # Regula încărcărilor s-a schimbat: recalculăm tot istoricul o singură dată.
                analyze_charges(con, vin, 0, time.time())
            analyze(con, vin, days)
        if redo:
            con.execute("INSERT INTO analysis_meta VALUES ('charges_version', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (CHARGES_VERSION,))
            print(f"analiza: încărcările recalculate după regula nouă (v{CHARGES_VERSION})", flush=True)


# ---------- locuri și adrese ----------

def _dist_m(a_lat, a_lon, b_lat, b_lon):
    k = math.pi / 180
    x = (b_lon - a_lon) * k * math.cos((a_lat + b_lat) * k / 2)
    y = (b_lat - a_lat) * k
    return math.hypot(x, y) * 6371000


def label_for(con, lat, lon):
    if lat is None or lon is None:
        return None
    for p in con.execute("SELECT name, lat, lon, radius_m FROM places"):
        if _dist_m(lat, lon, p[1], p[2]) <= (p[3] or 150):
            return p[0]
    r = con.execute("SELECT label FROM geocache WHERE key=?", (f"{lat:.4f},{lon:.4f}",)).fetchone()
    return r[0] if r else None


async def geocode_pending(db, limit=8):
    """Adresele pentru capetele de drum și locurile de încărcare (OpenStreetMap, max. 1 cerere/s)."""
    with db() as con:
        pts = set()
        for r in con.execute("SELECT start_lat, start_lon, end_lat, end_lon FROM drives ORDER BY start_ts DESC LIMIT 300"):
            pts.add((r[0], r[1]))
            pts.add((r[2], r[3]))
        for r in con.execute("SELECT lat, lon FROM charges ORDER BY start_ts DESC LIMIT 200"):
            pts.add((r[0], r[1]))
        todo = []
        for la, lo in pts:
            if la is None or lo is None:
                continue
            key = f"{la:.4f},{lo:.4f}"
            if not con.execute("SELECT 1 FROM geocache WHERE key=?", (key,)).fetchone():
                todo.append((key, la, lo))
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": GEOCODE_UA}) as c:
        for key, la, lo in todo[:limit]:
            try:
                r = await c.get("https://nominatim.openstreetmap.org/reverse", params={
                    "format": "jsonv2", "lat": la, "lon": lo, "zoom": 17, "accept-language": "ro"})
                a = r.json().get("address", {}) if r.status_code == 200 else {}
            except (httpx.HTTPError, ValueError):
                a = None
            if a is None:
                break
            road = a.get("road") or a.get("pedestrian") or a.get("suburb") or a.get("neighbourhood")
            if road and a.get("house_number"):
                road = f"{road} {a['house_number']}"
            town = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("county")
            label = ", ".join(x for x in (road, town) if x) or "Loc necunoscut"
            with db() as con:
                con.execute("INSERT OR REPLACE INTO geocache VALUES (?,?)", (key, label))
            await asyncio.sleep(1.2)


# ---------- costuri Supercharger ----------

def _iso_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


async def sync_supercharger(db, tesla, vin, acc_id):
    r = await tesla(acc_id, "GET", "/api/1/dx/charging/history", params={"vin": vin, "pageSize": 50})
    if r.status_code != 200:
        return
    try:
        items = r.json().get("data") or []
    except ValueError:
        return
    with db() as con:
        for it in items:
            fees = it.get("fees") or []
            cost = sum(float(f.get("totalDue") or 0) for f in fees)
            cur = next((f.get("currencyCode") for f in fees if f.get("currencyCode")), None)
            con.execute(
                "INSERT OR REPLACE INTO sc_sessions VALUES (?,?,?,?,?,?,?)",
                (str(it.get("sessionId")), it.get("vin") or vin, _iso_ts(it.get("chargeStartDateTime")),
                 _iso_ts(it.get("chargeStopDateTime")), it.get("siteLocationName"), cost, cur),
            )


def charge_cost(con, c):
    """(cost, monedă, loc Supercharger). Acasă/AC = 0, DC = factura Tesla sau costul introdus manual."""
    if c["manual_cost"] is not None:
        return c["manual_cost"], c["manual_currency"] or "RON", None
    if c["dc"]:
        s = con.execute(
            "SELECT cost, currency, site FROM sc_sessions WHERE vin=? AND ABS(start_ts - ?) < 2700 "
            "ORDER BY ABS(start_ts - ?) LIMIT 1",
            (c["vin"], c["start_ts"], c["start_ts"]),
        ).fetchone()
        if s:
            return s[0], s[1] or "RON", s[2]
        return None, None, None
    return 0.0, "RON", None


# ---------- formatarea pentru aplicație ----------

def drive_dict(con, d):
    end = d["end_ts"] or time.time()
    km = (d["end_odo"] - d["start_odo"]) * MILES if d["start_odo"] is not None and d["end_odo"] is not None else None
    kwh = (d["start_energy"] - d["end_energy"]) if d["start_energy"] is not None and d["end_energy"] is not None else None
    hours = max(end - d["start_ts"], 1) / 3600
    return {
        "id": d["id"], "start_ts": d["start_ts"], "end_ts": d["end_ts"], "ongoing": d["end_ts"] is None,
        "km": round(km, 2) if km is not None else None,
        "duration_min": round((end - d["start_ts"]) / 60),
        "kwh": round(kwh, 2) if kwh is not None else None,
        "kwh_100km": round(kwh / km * 100, 1) if kwh is not None and km and km >= 0.5 and kwh >= 0 else None,
        "soc_start": d["start_soc"], "soc_end": d["end_soc"],
        "range_used_km": round((d["start_range"] - d["end_range"]) * MILES, 1)
        if d["start_range"] is not None and d["end_range"] is not None else None,
        "avg_speed": round(km / hours) if km else None,
        "max_speed": round(d["max_speed"] * MILES) if d["max_speed"] else None,
        "out_temp": round(d["out_temp"], 1) if d["out_temp"] is not None else None,
        "from": label_for(con, d["start_lat"], d["start_lon"]),
        "to": label_for(con, d["end_lat"], d["end_lon"]),
        "start": [d["start_lat"], d["start_lon"]], "end": [d["end_lat"], d["end_lon"]],
    }


def charge_dict(con, c):
    end = c["end_ts"] or time.time()
    gain = (c["end_energy"] - c["start_energy"]) if c["start_energy"] is not None and c["end_energy"] is not None else None
    e_in = c["energy_in"]
    cost, cur, site = charge_cost(con, c)
    # Puterea medie se raportează la timpul în care chiar s-a încărcat, nu la cât a stat cuplată.
    secs = c["charge_seconds"] if c["charge_seconds"] else end - c["start_ts"]
    hours = max(secs, 1) / 3600
    return {
        "id": c["id"], "start_ts": c["start_ts"], "end_ts": c["end_ts"], "ongoing": c["end_ts"] is None,
        "duration_min": round((end - c["start_ts"]) / 60),
        "charging_min": round(c["charge_seconds"] / 60) if c["charge_seconds"] else None,
        "soc_start": c["start_soc"], "soc_end": c["end_soc"],
        "battery_kwh": round(gain, 2) if gain is not None else None,
        "charger_kwh": round(e_in, 2) if e_in else None,
        "efficiency": round(gain / e_in * 100) if gain and e_in and e_in > 0.5 and 0 < gain <= e_in * 1.05 else None,
        "max_kw": round(c["max_power"], 1) if c["max_power"] else None,
        "avg_kw": round((e_in or gain or 0) / hours, 1) if (e_in or gain) else None,
        "dc": bool(c["dc"]), "cost": round(cost, 2) if cost is not None else None, "currency": cur,
        "place": site or label_for(con, c["lat"], c["lon"]),
        "lat": c["lat"], "lon": c["lon"], "manual_cost": c["manual_cost"] is not None,
    }


def parks(con, vin, t0, t1):
    """Perioadele de parcare dintre drumuri, cu energia pierdută (fără energia adăugată la încărcare)."""
    drives = con.execute(
        "SELECT * FROM drives WHERE vin=? AND end_ts IS NOT NULL ORDER BY start_ts", (vin,)
    ).fetchall()
    out = []
    for i, d in enumerate(drives):
        p0 = d["end_ts"]
        nxt = drives[i + 1] if i + 1 < len(drives) else None
        ongoing_drive = con.execute(
            "SELECT start_ts, start_energy FROM drives WHERE vin=? AND end_ts IS NULL AND start_ts>?", (vin, p0)
        ).fetchone()
        if nxt:
            p1, e1 = nxt["start_ts"], nxt["start_energy"]
        elif ongoing_drive:
            p1, e1 = ongoing_drive[0], ongoing_drive[1]
        else:
            p1, e1 = None, value_at(con, vin, "EnergyRemaining", time.time())
        pe = p1 or time.time()
        if pe < t0 or p0 > t1:
            continue
        gain = sum(
            (c[1] - c[0]) for c in con.execute(
                "SELECT start_energy, end_energy FROM charges WHERE vin=? AND start_ts>=? AND start_ts<? "
                "AND start_energy IS NOT NULL AND end_energy IS NOT NULL", (vin, p0, pe))
        )
        e0 = d["end_energy"]
        lost = (e0 + gain - e1) if e0 is not None and e1 is not None else None
        sentry = con.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE vin=? AND field='SentryMode' AND ts BETWEEN ? AND ? "
            "AND value NOT IN ('SentryModeStateOff','SentryModeStateUnknown')", (vin, p0, pe)
        ).fetchone()[0]
        hours = (pe - p0) / 3600
        out.append({
            "start_ts": p0, "end_ts": p1, "ongoing": p1 is None, "hours": round(hours, 1),
            "kwh_lost": round(max(lost, 0), 2) if lost is not None else None,
            "kwh_per_day": round(max(lost, 0) / hours * 24, 2) if lost is not None and hours >= 1 else None,
            "charged": gain > 0.2, "sentry": sentry > 0,
            "place": label_for(con, d["end_lat"], d["end_lon"]),
        })
    return list(reversed(out))


def day_bounds(day_from: str, day_to: str, tz):
    d0 = datetime.fromisoformat(day_from).replace(tzinfo=tz)
    d1 = datetime.fromisoformat(day_to).replace(tzinfo=tz) + timedelta(days=1)
    return d0.timestamp(), d1.timestamp()


def stats(con, vin, day_from, day_to, tz):
    t0, t1 = day_bounds(day_from, day_to, tz)
    days = days_between(day_from, day_to)
    per ={k: {"day": k, "km": 0.0, "kwh": 0.0, "km_e": 0.0, "standby_kwh": 0.0} for k in days}

    def day_of(ts):
        return datetime.fromtimestamp(ts, tz).date().isoformat()

    drives = [drive_dict(con, r) for r in con.execute(
        "SELECT * FROM drives WHERE vin=? AND start_ts>=? AND start_ts<? ORDER BY start_ts", (vin, t0, t1))]
    km = kwh = km_e = mins = 0.0
    longest = None
    for dr in drives:
        k = day_of(dr["start_ts"])
        if dr["km"]:
            km += dr["km"]
            per[k]["km"] += dr["km"]
            if not longest or dr["km"] > longest:
                longest = dr["km"]
        if dr["kwh"] is not None and dr["km"] and dr["kwh"] >= 0:
            kwh += dr["kwh"]
            km_e += dr["km"]
            per[k]["kwh"] += dr["kwh"]
            per[k]["km_e"] += dr["km"]
        mins += dr["duration_min"]

    charges = [charge_dict(con, r) for r in con.execute(
        "SELECT * FROM charges WHERE vin=? AND start_ts>=? AND start_ts<? ORDER BY start_ts", (vin, t0, t1))]
    charged_batt = sum(c["battery_kwh"] or 0 for c in charges)
    charged_in = sum(c["charger_kwh"] or c["battery_kwh"] or 0 for c in charges)
    costs = {}
    for c in charges:
        if c["cost"]:
            costs[c["currency"]] = costs.get(c["currency"], 0) + c["cost"]
    eff = [(c["battery_kwh"], c["charger_kwh"]) for c in charges if c["efficiency"]]

    standby = 0.0
    for p in parks(con, vin, t0, t1):
        if not p["kwh_lost"]:
            continue
        p0, p1 = p["start_ts"], p["end_ts"] or time.time()
        span = max(p1 - p0, 1)
        for k in days:
            a, b = day_bounds(k, k, tz)
            ov = max(0, min(b, p1) - max(a, p0))
            if ov > 0:
                part = p["kwh_lost"] * ov / span
                per[k]["standby_kwh"] += part
                standby += part

    daily = []
    for k in days:
        x = per[k]
        daily.append({
            "day": k, "km": round(x["km"], 1), "kwh": round(x["kwh"], 2),
            "kwh_100km": round(x["kwh"] / x["km_e"] * 100, 1) if x["km_e"] >= 1 else None,
            "standby_kwh": round(x["standby_kwh"], 2),
        })
    hours = mins / 60
    return {
        "from": day_from, "to": day_to,
        "totals": {
            "km": round(km, 1), "drives": len(drives), "kwh": round(kwh, 1),
            "kwh_100km": round(kwh / km_e * 100, 1) if km_e >= 1 else None,
            "drive_minutes": round(mins), "avg_speed": round(km / hours) if hours > 0.05 else None,
            "longest_km": round(longest, 1) if longest else None,
            "charges": len(charges), "dc_charges": sum(1 for c in charges if c["dc"]),
            "charged_kwh": round(charged_in, 1), "charged_battery_kwh": round(charged_batt, 1),
            "charge_efficiency": round(sum(a for a, _ in eff) / sum(b for _, b in eff) * 100) if eff else None,
            "standby_kwh": round(standby, 1), "costs": {k: round(v, 2) for k, v in costs.items()},
            "cost_per_100km": {k: round(v / km * 100, 2) for k, v in costs.items()} if km >= 1 else {},
        },
        "daily": daily,
    }


def health(con, vin):
    s = con.execute("SELECT nominal_kwh FROM vehicle_settings WHERE vin=?", (vin,)).fetchone()
    nominal = (s[0] if s and s[0] else None) or DEFAULT_KWH
    rows = con.execute("SELECT day, kwh FROM capacity_log WHERE vin=? ORDER BY day", (vin,)).fetchall()
    hist = [[r[0], round(r[1], 1)] for r in rows]
    recent = sorted(r[1] for r in rows[-7:])
    cap = recent[len(recent) // 2] if recent else None
    return {
        "capacity_kwh": round(cap, 1) if cap else None, "nominal_kwh": nominal,
        "health_pct": round(min(cap / nominal * 100, 100), 1) if cap else None, "history": hist,
    }


def today(con, vin, tz):
    k = datetime.now(tz).date().isoformat()
    return stats(con, vin, k, k, tz)["daily"][0]
