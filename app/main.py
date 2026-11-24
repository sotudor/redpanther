import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlencode

import httpx
import base64
from fastapi import FastAPI, Form, Request, UploadFile, File
import analysis
import telemetry
import twofa
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

ENV = os.environ
CLIENT_ID = ENV["TESLA_CLIENT_ID"]
CLIENT_SECRET = ENV["TESLA_CLIENT_SECRET"]
APP_PASSWORD = ENV["APP_PASSWORD"]
SESSION_SECRET = ENV["SESSION_SECRET"].encode()
BASE_URL = ENV["BASE_URL"].rstrip("/")
try:
    ORIGINAL_RANGE_KM = float(ENV.get("ORIGINAL_RANGE_KM") or 0)
except ValueError:
    ORIGINAL_RANGE_KM = 0.0
try:
    BATTERY_KWH = float(ENV.get("BATTERY_KWH") or 75)
except ValueError:
    BATTERY_KWH = 75.0
POLL_MINUTES = float(ENV.get("POLL_MINUTES") or 30)
ACTIVE_POLL_MINUTES = float(ENV.get("ACTIVE_POLL_MINUTES") or 10)
TZ = ZoneInfo(ENV.get("TZ_NAME") or "Europe/Bucharest")
CHARGING = {"Charging", "Starting"}

AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
API = ENV.get("TESLA_API", "https://fleet-api.prd.eu.vn.cloud.tesla.com")
SCOPES = "openid offline_access vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds"
REDIRECT_URI = BASE_URL + "/auth/callback"
ENDPOINTS = "charge_state;climate_state;drive_state;vehicle_config;vehicle_state"

DB = "/data/app.db"
MILES = 1.609344
SESSION_DAYS = 90
STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"manifest.json", "sw.js", "icon-192.png", "icon-512.png", "login.html"}
PUBLIC_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"
PUBLIC_PATHS = {PUBLIC_KEY_PATH, "/login", "/login/code", "/manifest.json", "/sw.js", "/icon-192.png", "/icon-512.png"}
PRE_AUTH_MINUTES = 5      # cât ai la dispoziție să introduci codul 2FA după parolă


class TeslaError(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


# ---------- baza de date ----------

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                access_token TEXT, refresh_token TEXT, expires_at REAL
            );
            CREATE TABLE IF NOT EXISTS vehicles (
                vin TEXT PRIMARY KEY, account_id INTEGER, name TEXT,
                state TEXT, data TEXT, data_ts REAL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                vin TEXT, ts REAL, odometer_km REAL, battery_level REAL,
                usable_level REAL, range_km REAL, charging_state TEXT, shift TEXT
            );
            CREATE INDEX IF NOT EXISTS snapshots_vin_ts ON snapshots(vin, ts);
            CREATE TABLE IF NOT EXISTS login_failures (ip TEXT, ts REAL);
            CREATE INDEX IF NOT EXISTS login_failures_ts ON login_failures(ts);
            CREATE INDEX IF NOT EXISTS login_failures_ip ON login_failures(ip, ts);
            CREATE TABLE IF NOT EXISTS login_blocks (
                ip TEXT PRIMARY KEY, until REAL, strikes INTEGER
            );
            CREATE TABLE IF NOT EXISTS auth_state (k TEXT PRIMARY KEY, v TEXT);
            """
        )
        telemetry.init_db(con)
        analysis.init_db(con)
        twofa.init_db(con)


init_db()


# ---------- sesiune ----------

def sign(value: str) -> str:
    mac = hmac.new(SESSION_SECRET, value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{mac}"


def unsign(token: str):
    if not token or "." not in token:
        return None
    value, _, mac = token.rpartition(".")
    good = hmac.new(SESSION_SECRET, value.encode(), hashlib.sha256).hexdigest()
    return value if hmac.compare_digest(mac, good) else None


# „Generația” sesiunilor: la activarea sau dezactivarea 2FA crește, iar toate sesiunile
# deschise înainte (pe alte dispozitive) nu mai sunt valabile.
def _load_epoch() -> int:
    with db() as con:
        r = con.execute("SELECT v FROM auth_state WHERE k='session_epoch'").fetchone()
    return int(r[0]) if r else 0


SESSION_EPOCH = _load_epoch()


def bump_session_epoch(con):
    global SESSION_EPOCH
    SESSION_EPOCH += 1
    con.execute("INSERT INTO auth_state VALUES ('session_epoch', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(SESSION_EPOCH),))


def valid_session(request: Request) -> bool:
    # Formatul cookie-ului: „expirare:generație” (sesiunile vechi, fără generație, sunt generația 0).
    v = unsign(request.cookies.get("rp_session", ""))
    if v is None:
        return False
    exp, _, epoch = v.partition(":")
    try:
        return float(exp) > time.time() and int(epoch or 0) == SESSION_EPOCH
    except ValueError:
        return False


def set_session(resp):
    exp = time.time() + SESSION_DAYS * 86400
    resp.set_cookie("rp_session", sign(f"{exp}:{SESSION_EPOCH}"), max_age=SESSION_DAYS * 86400,
                    httponly=True, secure=True, samesite="lax")
    return resp


def valid_pre_auth(request: Request) -> bool:
    """Parola a fost corectă, dar codul 2FA încă nu a fost introdus."""
    v = unsign(request.cookies.get("rp_pre", ""))
    if not v or not v.startswith("pre:"):
        return False
    try:
        return float(v[4:]) > time.time()
    except ValueError:
        return False


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or valid_session(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "auth"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def message_page(title: str, text: str) -> HTMLResponse:
    body = f"""<!doctype html><html lang="ro"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1.25rem;line-height:1.5}}
a{{color:#a3202e}}</style></head><body><h1>{html.escape(title)}</h1>
<p>{html.escape(text)}</p><p><a href="/">Înapoi la aplicație</a></p></body></html>"""
    return HTMLResponse(body)


# ---------- login aplicație ----------

# Protecția împotriva încercărilor repetate de parolă. Contorii stau în SQLite,
# deci rezistă la repornirea aplicației.
IP_MAX_FAILS = 5                # încercări greșite permise de pe o adresă...
IP_WINDOW = 15 * 60             # ...în 15 minute
BLOCK_BASE = 15 * 60            # prima blocare: 15 minute, apoi se dublează
BLOCK_MAX = 24 * 3600           # cel mult 24 de ore
STRIKES_FORGET = 24 * 3600      # după o zi fără blocări, dublarea o ia de la capăt
GLOBAL_MAX_FAILS = 30           # încercări greșite de pe toate adresele...
GLOBAL_WINDOW = 3600            # ...într-o oră


def client_ip(request: Request) -> str:
    # Aplicația e accesibilă doar prin Caddy, care pune adresa reală a clientului
    # la finalul lui X-Forwarded-For; ce e înainte poate fi trimis de oricine.
    xff = request.headers.get("x-forwarded-for", "")
    last = xff.split(",")[-1].strip()
    if last:
        return last
    return request.client.host if request.client else "necunoscut"


def login_block(con, ip: str, now: float):
    """Întoarce (până_când, global) dacă login-ul e blocat acum, altfel None."""
    b = con.execute("SELECT until FROM login_blocks WHERE ip=?", (ip,)).fetchone()
    if b and b["until"] > now:
        return b["until"], False
    n = con.execute("SELECT count(*) FROM login_failures WHERE ts>?", (now - GLOBAL_WINDOW,)).fetchone()[0]
    if n >= GLOBAL_MAX_FAILS:
        # Blocarea ține până când în ultima oră rămân sub 30 de încercări greșite.
        oldest = con.execute(
            "SELECT ts FROM login_failures WHERE ts>? ORDER BY ts LIMIT 1 OFFSET ?",
            (now - GLOBAL_WINDOW, n - GLOBAL_MAX_FAILS),
        ).fetchone()[0]
        return oldest + GLOBAL_WINDOW, True
    return None


def record_failure(con, ip: str, now: float) -> int:
    """Notează o parolă greșită. Întoarce câte încercări mai are adresa (0 = tocmai a fost blocată)."""
    con.execute("DELETE FROM login_failures WHERE ts<?", (now - max(GLOBAL_WINDOW, IP_WINDOW),))
    con.execute("INSERT INTO login_failures VALUES (?,?)", (ip, now))
    b = con.execute("SELECT until, strikes FROM login_blocks WHERE ip=?", (ip,)).fetchone()
    since = max(now - IP_WINDOW, b["until"] if b else 0)   # încercările de dinaintea ultimei blocări nu mai contează
    n = con.execute("SELECT count(*) FROM login_failures WHERE ip=? AND ts>?", (ip, since)).fetchone()[0]
    if n < IP_MAX_FAILS:
        return IP_MAX_FAILS - n
    strikes = (b["strikes"] if b and now - b["until"] < STRIKES_FORGET else 0) + 1
    until = now + min(BLOCK_BASE * 2 ** (strikes - 1), BLOCK_MAX)
    con.execute(
        "INSERT INTO login_blocks VALUES (?,?,?) ON CONFLICT(ip) DO UPDATE SET until=excluded.until, strikes=excluded.strikes",
        (ip, until, strikes),
    )
    print(f"login: adresa {ip} blocată {round((until - now) / 60)} min (blocarea nr. {strikes})", flush=True)
    return 0


def blocked_redirect(until: float, is_global: bool, now: float) -> RedirectResponse:
    q = urlencode({"b": max(1, round(until - now)), **({"g": 1} if is_global else {})})
    return RedirectResponse(f"/login?{q}", status_code=303)


@app.get("/login")
async def login_page():
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})


async def failed_attempt(ip: str, now: float, retry_url: str) -> RedirectResponse:
    """O parolă sau un cod 2FA greșit: intră în aceleași contoare împotriva ghicirii."""
    with db() as con:
        left = record_failure(con, ip, now)
        blk = login_block(con, ip, now)
    await asyncio.sleep(1.5)
    if blk:
        return blocked_redirect(*blk, now)
    return RedirectResponse(f"{retry_url}e=1&r={left}", status_code=303)


def login_success(ip: str) -> RedirectResponse:
    with db() as con:
        con.execute("DELETE FROM login_failures WHERE ip=?", (ip,))
        con.execute("DELETE FROM login_blocks WHERE ip=?", (ip,))
    resp = set_session(RedirectResponse("/", status_code=303))
    resp.delete_cookie("rp_pre")
    return resp


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    ip, now = client_ip(request), time.time()
    with db() as con:
        blk = login_block(con, ip, now)
    if blk:
        # Cât timp e blocat, parola nici nu se mai verifică.
        return blocked_redirect(*blk, now)
    if not hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
        return await failed_attempt(ip, now, "/login?")
    with db() as con:
        need_code = twofa.enabled(con)
    if not need_code:
        return login_success(ip)
    # Parola e bună; urmează codul din aplicația de autentificare.
    resp = RedirectResponse("/login?step=code", status_code=303)
    resp.set_cookie("rp_pre", sign(f"pre:{now + PRE_AUTH_MINUTES * 60}"), max_age=PRE_AUTH_MINUTES * 60,
                    httponly=True, secure=True, samesite="strict")
    return resp


@app.post("/login/code")
async def login_code(request: Request, code: str = Form(...)):
    ip, now = client_ip(request), time.time()
    with db() as con:
        blk = login_block(con, ip, now)
    if blk:
        return blocked_redirect(*blk, now)
    if not valid_pre_auth(request):
        return RedirectResponse("/login?x=1", status_code=303)
    with db() as con:
        kind = twofa.verify(con, code, now)
    if not kind:
        return await failed_attempt(ip, now, "/login?step=code&")
    if kind == "recovery":
        print(f"login: cod de rezervă folosit de pe {ip}", flush=True)
    return login_success(ip)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("rp_session")
    resp.delete_cookie("rp_pre")
    return resp


# ---------- 2FA din Setări ----------

async def _code_from(request: Request) -> str:
    try:
        return str((await request.json()).get("code") or "")
    except ValueError:
        return ""


async def _settings_failure(ip: str, now: float, message: str) -> JSONResponse:
    """Codurile greșite din Setări intră și ele în contoare (cineva cu un telefon furat
    și sesiunea deschisă nu poate ghici codul ca să dezactiveze 2FA)."""
    with db() as con:
        record_failure(con, ip, now)
        blk = login_block(con, ip, now)
    await asyncio.sleep(1.5)
    if blk:
        return JSONResponse({"ok": False, "message": f"Prea multe coduri greșite. Încearcă din nou peste {max(1, round((blk[0] - now) / 60))} min."}, status_code=429)
    return JSONResponse({"ok": False, "message": message}, status_code=400)


def _blocked_json(ip: str, now: float):
    with db() as con:
        blk = login_block(con, ip, now)
    if blk:
        return JSONResponse({"ok": False, "message": f"Prea multe încercări greșite. Încearcă din nou peste {max(1, round((blk[0] - now) / 60))} min."}, status_code=429)
    return None


@app.get("/api/2fa")
async def api_2fa():
    with db() as con:
        return twofa.status(con)


@app.post("/api/2fa/setup")
async def api_2fa_setup():
    with db() as con:
        if twofa.enabled(con):
            return JSONResponse({"ok": False, "message": "Autentificarea în doi pași e deja activă."}, status_code=400)
        secret = twofa.start_setup(con, time.time())
    account = BASE_URL.split("://", 1)[-1]
    return {"ok": True, "secret": secret, "uri": twofa.otpauth_uri(secret, account),
            "minutes": twofa.SETUP_MINUTES}


@app.post("/api/2fa/confirm")
async def api_2fa_confirm(request: Request):
    ip, now = client_ip(request), time.time()
    if (b := _blocked_json(ip, now)):
        return b
    code = await _code_from(request)
    with db() as con:
        if twofa.enabled(con):
            return JSONResponse({"ok": False, "message": "Autentificarea în doi pași e deja activă."}, status_code=400)
        if twofa.setup_expired(con, now):
            return JSONResponse({"ok": False, "expired": True, "message": "Codul QR a expirat. Începe din nou activarea."}, status_code=400)
        codes = twofa.confirm_setup(con, code, now)
        if codes:
            bump_session_epoch(con)
    if not codes:
        return await _settings_failure(ip, now, "Codul nu se potrivește. Verifică ora telefonului și încearcă un cod nou.")
    print("2fa: activată; sesiunile de pe alte dispozitive au fost închise", flush=True)
    # Sesiunea de pe dispozitivul curent rămâne deschisă, cu noua generație.
    return set_session(JSONResponse({"ok": True, "recovery": codes}))


@app.post("/api/2fa/recovery")
async def api_2fa_recovery(request: Request):
    ip, now = client_ip(request), time.time()
    if (b := _blocked_json(ip, now)):
        return b
    code = await _code_from(request)
    with db() as con:
        ok = twofa.verify(con, code, now, allow_recovery=False)
        codes = twofa.new_recovery(con) if ok else None
    if not codes:
        return await _settings_failure(ip, now, "Codul din aplicație nu e corect.")
    return {"ok": True, "recovery": codes}


@app.post("/api/2fa/disable")
async def api_2fa_disable(request: Request):
    ip, now = client_ip(request), time.time()
    if (b := _blocked_json(ip, now)):
        return b
    code = await _code_from(request)
    with db() as con:
        ok = twofa.verify(con, code, now)
        if ok:
            twofa.disable(con)
            bump_session_epoch(con)
    if not ok:
        return await _settings_failure(ip, now, "Codul nu e corect.")
    print("2fa: dezactivată; sesiunile de pe alte dispozitive au fost închise", flush=True)
    return set_session(JSONResponse({"ok": True}))


# ---------- Tesla: tokenuri ----------

locks: dict[int, asyncio.Lock] = {}


async def token_for(acc_id: int) -> str:
    lock = locks.setdefault(acc_id, asyncio.Lock())
    async with lock:
        with db() as con:
            a = con.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not a:
            raise TeslaError("Contul Tesla nu mai există. Conectează-l din nou.")
        if a["expires_at"] - 300 > time.time():
            return a["access_token"]
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": a["refresh_token"],
            })
        if r.status_code != 200:
            raise TeslaError("Autorizarea Tesla a expirat. Conectează contul din nou.")
        t = r.json()
        with db() as con:
            con.execute(
                "UPDATE accounts SET access_token=?, refresh_token=?, expires_at=? WHERE id=?",
                (t["access_token"], t.get("refresh_token", a["refresh_token"]),
                 time.time() + t.get("expires_in", 28800), acc_id),
            )
        return t["access_token"]


async def tesla(acc_id: int, method: str, path: str, **kw) -> httpx.Response:
    tok = await token_for(acc_id)
    async with httpx.AsyncClient(timeout=30) as c:
        return await c.request(method, API + path,
                               headers={"Authorization": f"Bearer {tok}"}, **kw)


async def sync_vehicles(acc_id: int):
    r = await tesla(acc_id, "GET", "/api/1/vehicles")
    if r.status_code != 200:
        raise TeslaError(f"Lista de mașini nu s-a putut citi (cod {r.status_code}).")
    found = r.json().get("response", []) or []
    with db() as con:
        for v in found:
            con.execute(
                """INSERT INTO vehicles(vin, account_id, name, state) VALUES (?,?,?,?)
                   ON CONFLICT(vin) DO UPDATE SET account_id=excluded.account_id,
                   name=excluded.name, state=excluded.state""",
                (v["vin"], acc_id, v.get("display_name") or "Tesla", v.get("state")),
            )
    return found


# ---------- Tesla: autorizare ----------

@app.get("/auth/tesla")
async def auth_start():
    state = secrets.token_urlsafe(24)
    q = urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "prompt": "login",
        "prompt_missing_scopes": "true",
    })
    resp = RedirectResponse(f"{AUTH_URL}?{q}", status_code=303)
    resp.set_cookie("rp_state", sign(state), max_age=600, httponly=True,
                    secure=True, samesite="lax")
    return resp


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return message_page("Conectarea nu s-a făcut",
                            f"Tesla a răspuns: {error or 'fără cod de autorizare'}. Încearcă din nou.")
    saved = unsign(request.cookies.get("rp_state", ""))
    if not saved or not hmac.compare_digest(saved, state):
        return message_page("Conectarea a expirat",
                            "Pornește din nou conectarea contului Tesla din aplicație.")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "audience": API,
            "redirect_uri": REDIRECT_URI,
        })
    if r.status_code != 200:
        return message_page("Tesla a refuzat autorizarea", r.text[:300])
    t = r.json()
    with db() as con:
        cur = con.execute(
            "INSERT INTO accounts(access_token, refresh_token, expires_at) VALUES (?,?,?)",
            (t["access_token"], t.get("refresh_token"), time.time() + t.get("expires_in", 28800)),
        )
        acc_id = cur.lastrowid
    try:
        await sync_vehicles(acc_id)
    except (TeslaError, httpx.HTTPError):
        pass
    with db() as con:
        con.execute(
            "DELETE FROM accounts WHERE id != ? AND id NOT IN (SELECT DISTINCT account_id FROM vehicles)",
            (acc_id,),
        )
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("rp_state")
    return resp


# ---------- API pentru aplicație ----------

def km(miles):
    return round(miles * MILES, 1) if isinstance(miles, (int, float)) else None


def summarize(r: dict) -> dict:
    cs = r.get("charge_state") or {}
    cl = r.get("climate_state") or {}
    vs = r.get("vehicle_state") or {}
    vc = r.get("vehicle_config") or {}
    ds = r.get("drive_state") or {}

    level = cs.get("battery_level")
    rng = cs.get("battery_range")
    full = None
    if isinstance(level, (int, float)) and isinstance(rng, (int, float)) and level >= 20:
        full = round(rng * MILES / (level / 100))
    health = round(full / ORIGINAL_RANGE_KM * 100, 1) if full and ORIGINAL_RANGE_KM else None
    speed = ds.get("speed")

    return {
        "name": r.get("display_name"),
        "battery_level": level,
        "usable_battery_level": cs.get("usable_battery_level"),
        "range_km": km(rng),
        "est_range_km": km(cs.get("est_battery_range")),
        "charge_limit": cs.get("charge_limit_soc"),
        "charging_state": cs.get("charging_state"),
        "charger_power_kw": cs.get("charger_power"),
        "charge_rate_kmh": km(cs.get("charge_rate")),
        "minutes_to_full": cs.get("minutes_to_full_charge"),
        "energy_added_kwh": cs.get("charge_energy_added"),
        "charger_voltage": cs.get("charger_voltage"),
        "charger_current": cs.get("charger_actual_current"),
        "fast_charger": cs.get("fast_charger_present"),
        "inside_temp": cl.get("inside_temp"),
        "outside_temp": cl.get("outside_temp"),
        "climate_on": cl.get("is_climate_on"),
        "odometer_km": km(vs.get("odometer")),
        "locked": vs.get("locked"),
        "sentry": vs.get("sentry_mode"),
        "software": vs.get("car_version"),
        "user_present": vs.get("is_user_present"),
        "tpms": {k: vs.get(f"tpms_pressure_{k}") for k in ("fl", "fr", "rl", "rr")},
        "car_type": vc.get("car_type"),
        "trim": vc.get("trim_badging"),
        "shift": ds.get("shift_state"),
        "speed_kmh": round(speed * MILES) if isinstance(speed, (int, float)) else None,
        "full_range_km": full,
        "original_range_km": ORIGINAL_RANGE_KM or None,
        "health_pct": health,
    }


def vehicle_row(vin: str):
    with db() as con:
        return con.execute("SELECT * FROM vehicles WHERE vin=?", (vin,)).fetchone()


def cached(row, status: str, message=None):
    data = json.loads(row["data"]) if row["data"] else None
    return {"status": status, "message": message, "ts": row["data_ts"], "data": data}


@app.get("/api/vehicles")
async def api_vehicles():
    with db() as con:
        accs = [r["id"] for r in con.execute("SELECT id FROM accounts")]
    errors = []
    for a in accs:
        try:
            await sync_vehicles(a)
        except TeslaError as e:
            errors.append(e.msg)
        except httpx.HTTPError:
            errors.append("Serverele Tesla nu răspund acum.")
    with db() as con:
        rows = con.execute("SELECT vin, name, state FROM vehicles ORDER BY name").fetchall()
    return {"vehicles": [dict(r) for r in rows], "errors": errors}


async def fetch_vehicle(vin: str, acc_id: int):
    """Citește datele mașinii. Întoarce (status, date sau mesaj)."""
    r = await tesla(acc_id, "GET", f"/api/1/vehicles/{vin}/vehicle_data",
                    params={"endpoints": ENDPOINTS})
    if r.status_code == 408:
        with db() as con:
            con.execute("UPDATE vehicles SET state='asleep' WHERE vin=?", (vin,))
        return "asleep", None
    if r.status_code != 200:
        return "error", f"Tesla a răspuns cu codul {r.status_code}."
    data = summarize(r.json().get("response") or {})
    now = time.time()
    with db() as con:
        con.execute("UPDATE vehicles SET data=?, data_ts=?, state='online' WHERE vin=?",
                    (json.dumps(data), now, vin))
        if data.get("odometer_km") is not None and data.get("range_km") is not None:
            con.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
                (vin, now, data["odometer_km"], data.get("battery_level"),
                 data.get("usable_battery_level"), data["range_km"],
                 data.get("charging_state"), data.get("shift")),
            )
    return "online", data


@app.get("/api/vehicle/{vin}")
async def api_vehicle(vin: str):
    row = vehicle_row(vin)
    if not row:
        return JSONResponse({"error": "Mașină necunoscută"}, status_code=404)
    try:
        status, result = await fetch_vehicle(vin, row["account_id"])
    except TeslaError as e:
        return cached(row, "error", e.msg)
    except httpx.HTTPError:
        return cached(row, "error", "Serverele Tesla nu răspund acum.")
    if status == "online":
        return {"status": "online", "message": None, "ts": time.time(), "data": result}
    return cached(vehicle_row(vin), status, result)


@app.get("/api/history/{vin}")
async def api_history(vin: str, days: int = 30):
    days = max(1, min(days, 366))
    since = time.time() - (days + 1) * 86400
    with db() as con:
        snaps = con.execute(
            "SELECT * FROM snapshots WHERE vin=? AND ts>=? ORDER BY ts", (vin, since)
        ).fetchall()
        first = con.execute("SELECT MIN(ts) AS t FROM snapshots WHERE vin=?", (vin,)).fetchone()["t"]

    per_day = {}
    for a, b in zip(snaps, snaps[1:]):
        dist = b["odometer_km"] - a["odometer_km"]
        if dist < 0:
            continue
        charged = (
            (b["battery_level"] or 0) > (a["battery_level"] or 0)
            or b["charging_state"] in CHARGING
        )
        energy = None
        if a["battery_level"]:
            full_km = a["range_km"] / (a["battery_level"] / 100)
            if full_km > 0:
                energy = (a["range_km"] - b["range_km"]) / full_km * BATTERY_KWH
        day = datetime.fromtimestamp(b["ts"], TZ).date().isoformat()
        rec = per_day.setdefault(day, {"km": 0.0, "kwh": 0.0, "km_measured": 0.0,
                                       "idle_kwh": 0.0, "partial": False})
        if dist >= 0.3:
            rec["km"] += dist
            if not charged and energy is not None and energy >= 0:
                rec["kwh"] += energy
                rec["km_measured"] += dist
            else:
                rec["partial"] = True
        elif not charged and energy is not None and energy > 0:
            rec["idle_kwh"] += energy

    out = []
    for day in sorted(per_day, reverse=True):
        r = per_day[day]
        cons = r["kwh"] / r["km_measured"] * 100 if r["km_measured"] >= 1 else None
        out.append({
            "day": day,
            "km": round(r["km"], 1),
            "kwh": round(r["kwh"], 2),
            "kwh_per_100km": round(cons, 1) if cons is not None else None,
            "idle_kwh": round(r["idle_kwh"], 2),
            "partial": r["partial"],
        })
    tot_km = sum(r["km"] for r in per_day.values())
    tot_kwh = sum(r["kwh"] for r in per_day.values())
    tot_meas = sum(r["km_measured"] for r in per_day.values())
    return {
        "days": out,
        "totals": {
            "km": round(tot_km, 1),
            "kwh": round(tot_kwh, 1),
            "kwh_per_100km": round(tot_kwh / tot_meas * 100, 1) if tot_meas >= 1 else None,
            "idle_kwh": round(sum(r["idle_kwh"] for r in per_day.values()), 1),
        },
        "battery_kwh": BATTERY_KWH,
        "collecting_since": first,
    }


# ---------- colectare automată ----------

async def poller():
    """Fără interogări repetate ale mașinii: datele vin prin telemetrie.
    Aici doar analizăm datele, completăm adresele, lista mașinilor și costurile Supercharger."""
    await asyncio.sleep(20)
    last_sync = last_sc = 0.0
    while True:
        try:
            analysis.analyze_all(db)
        except Exception as e:
            print("analiza:", repr(e), flush=True)
        try:
            await analysis.geocode_pending(db)
        except Exception as e:
            print("adrese:", repr(e), flush=True)
        now = time.time()
        with db() as con:
            accs = [r["id"] for r in con.execute("SELECT id FROM accounts")]
        if now - last_sync > 6 * 3600:
            last_sync = now
            for acc in accs:
                try:
                    await sync_vehicles(acc)
                except Exception as e:
                    print("lista mașini:", repr(e), flush=True)
        if now - last_sc > 12 * 3600:
            last_sc = now
            with db() as con:
                vs = con.execute("SELECT vin, account_id FROM vehicles").fetchall()
            for v in vs:
                try:
                    await analysis.sync_supercharger(db, tesla, v["vin"], v["account_id"])
                except Exception as e:
                    print("supercharger:", repr(e), flush=True)
        try:
            telemetry.prune(db)
        except Exception:
            pass
        await asyncio.sleep(120)


@app.on_event("startup")
async def start_poller():
    asyncio.create_task(poller())
    try:
        await telemetry.start(db)
    except Exception as e:  # aplicația merge și fără telemetrie
        print("telemetry: nu a pornit:", repr(e), flush=True)


@app.get("/api/telemetry/{vin}")
async def api_telemetry(vin: str, check: int = 0):
    row = vehicle_row(vin)
    if not row:
        return JSONResponse({"error": "Mașină necunoscută"}, status_code=404)
    out = telemetry.live(db, vin)
    out["config"] = None
    if check:
        try:
            r = await tesla(row["account_id"], "GET", f"/api/1/vehicles/{vin}/fleet_telemetry_config")
            out["config"] = r.json().get("response") if r.status_code == 200 else {"error": r.status_code}
        except (TeslaError, httpx.HTTPError) as e:
            out["config"] = {"error": getattr(e, "msg", "Tesla nu răspunde")}
    return out


@app.post("/api/telemetry/{vin}/enable")
async def api_telemetry_enable(vin: str):
    row = vehicle_row(vin)
    if not row:
        return JSONResponse({"error": "Mașină necunoscută"}, status_code=404)
    try:
        token = await token_for(row["account_id"])
        with_loc = "vehicle_location" in token_scopes(token)
        status, body = await telemetry.push_config(vin, token, with_loc)
    except TeslaError as e:
        return {"ok": False, "message": e.msg}
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Proxy-ul Tesla nu răspunde ({e.__class__.__name__}). Verifică serviciul proxy."}
    resp = body.get("response") if isinstance(body, dict) else None
    ok = status == 200 and isinstance(resp, dict) and resp.get("updated_vehicles", 0) >= 1
    return {"ok": ok, "status": status, "tesla": body, "location": with_loc}


def token_scopes(token: str) -> set:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        scp = json.loads(base64.urlsafe_b64decode(part)).get("scp") or []
        return set(scp if isinstance(scp, list) else str(scp).split())
    except (IndexError, ValueError):
        return set()


# ---------- API pentru ecranele aplicației ----------

def _range(frm: str, to: str):
    today_ = datetime.now(TZ).date()
    try:
        d1 = datetime.fromisoformat(to).date() if to else today_
        d0 = datetime.fromisoformat(frm).date() if frm else d1 - timedelta(days=6)
    except ValueError:
        d1, d0 = today_, today_ - timedelta(days=6)
    if d0 > d1:
        d0, d1 = d1, d0
    return d0.isoformat(), d1.isoformat()


@app.get("/api/car/{vin}")
async def api_car(vin: str):
    row = vehicle_row(vin)
    if not row:
        return JSONResponse({"error": "Mașină necunoscută"}, status_code=404)
    live = telemetry.live(db, vin)
    with db() as con:
        h = analysis.health(con, vin)
        t = analysis.today(con, vin, TZ)
        drv = con.execute("SELECT * FROM drives WHERE vin=? AND end_ts IS NULL", (vin,)).fetchone()
        chg = con.execute("SELECT * FROM charges WHERE vin=? AND end_ts IS NULL", (vin,)).fetchone()
        loc = con.execute("SELECT lat, lon, ts FROM telemetry_loc WHERE vin=? ORDER BY ts DESC LIMIT 1", (vin,)).fetchone()
        place = analysis.label_for(con, loc[0], loc[1]) if loc else None
        st = con.execute("SELECT has_photo FROM vehicle_settings WHERE vin=?", (vin,)).fetchone()
    state = "driving" if drv else "charging" if chg else "parked"
    return {
        "vin": vin, "name": row["name"], "state": state, "tesla_state": row["state"],
        "polled": json.loads(row["data"]) if row["data"] else None, "polled_ts": row["data_ts"],
        "live": live, "health": h, "today": t, "place": place,
        "has_photo": bool(st and st[0]),
    }


@app.post("/api/vehicle/{vin}/refresh")
async def api_refresh(vin: str):
    return await api_vehicle(vin)


@app.get("/api/drives/{vin}")
async def api_drives(vin: str, frm: str = "", to: str = ""):
    d0, d1 = _range(frm, to)
    t0, t1 = analysis.day_bounds(d0, d1, TZ)
    with db() as con:
        rows = con.execute("SELECT * FROM drives WHERE vin=? AND start_ts>=? AND start_ts<? ORDER BY start_ts DESC",
                           (vin, t0, t1)).fetchall()
        return {"from": d0, "to": d1, "drives": [analysis.drive_dict(con, r) for r in rows]}


@app.get("/api/drive/{drive_id}")
async def api_drive(drive_id: int):
    with db() as con:
        r = con.execute("SELECT * FROM drives WHERE id=?", (drive_id,)).fetchone()
        if not r:
            return JSONResponse({"error": "Drum inexistent"}, status_code=404)
        end = r["end_ts"] or time.time()
        path = con.execute("SELECT lat, lon FROM telemetry_loc WHERE vin=? AND ts BETWEEN ? AND ? ORDER BY ts",
                           (r["vin"], r["start_ts"] - 30, end + 30)).fetchall()
        out = analysis.drive_dict(con, r)
        out["path"] = [[p[0], p[1]] for p in path]
        out["speed"] = [[t, round(v * analysis.MILES)] for t, v in analysis.series(con, r["vin"], "VehicleSpeed", r["start_ts"], end)]
        return out


@app.get("/api/charges/{vin}")
async def api_charges(vin: str, frm: str = "", to: str = ""):
    d0, d1 = _range(frm, to)
    t0, t1 = analysis.day_bounds(d0, d1, TZ)
    with db() as con:
        rows = con.execute("SELECT * FROM charges WHERE vin=? AND start_ts>=? AND start_ts<? ORDER BY start_ts DESC",
                           (vin, t0, t1)).fetchall()
        return {"from": d0, "to": d1, "charges": [analysis.charge_dict(con, r) for r in rows]}


@app.get("/api/charge/{charge_id}")
async def api_charge(charge_id: int):
    with db() as con:
        r = con.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone()
        if not r:
            return JSONResponse({"error": "Încărcare inexistentă"}, status_code=404)
        end = r["end_ts"] or time.time()
        vin, t0 = r["vin"], r["start_ts"] - 60
        out = analysis.charge_dict(con, r)
        field = "DCChargingPower" if r["dc"] else "ACChargingPower"
        out["power"] = analysis.series(con, vin, field, t0, end + 60)
        out["temp"] = analysis.series(con, vin, "ModuleTempMax", t0, end + 60)
        out["soc"] = analysis.series(con, vin, "Soc", t0, end + 60)
        return out


@app.post("/api/charge/{charge_id}/cost")
async def api_charge_cost(charge_id: int, request: Request):
    body = await request.json()
    cost = body.get("cost")
    with db() as con:
        if cost is None or cost == "":
            con.execute("UPDATE charges SET manual_cost=NULL, manual_currency=NULL WHERE id=?", (charge_id,))
        else:
            con.execute("UPDATE charges SET manual_cost=?, manual_currency=? WHERE id=?",
                        (float(cost), body.get("currency") or "RON", charge_id))
    return {"ok": True}


@app.get("/api/parks/{vin}")
async def api_parks(vin: str, frm: str = "", to: str = ""):
    d0, d1 = _range(frm, to)
    t0, t1 = analysis.day_bounds(d0, d1, TZ)
    with db() as con:
        return {"from": d0, "to": d1, "parks": analysis.parks(con, vin, t0, t1)}


@app.get("/api/stats/{vin}")
async def api_stats(vin: str, frm: str = "", to: str = ""):
    d0, d1 = _range(frm, to)
    with db() as con:
        return analysis.stats(con, vin, d0, d1, TZ)


@app.post("/api/places")
async def api_add_place(request: Request):
    b = await request.json()
    name = str(b.get("name") or "").strip()[:60]
    if not name or b.get("lat") is None or b.get("lon") is None:
        return JSONResponse({"error": "Date incomplete"}, status_code=400)
    with db() as con:
        con.execute("INSERT INTO places (name, lat, lon, radius_m) VALUES (?,?,?,?)",
                    (name, float(b["lat"]), float(b["lon"]), float(b.get("radius_m") or 150)))
    return {"ok": True}


@app.post("/api/vehicle/{vin}/settings")
async def api_settings(vin: str, request: Request):
    b = await request.json()
    kwh = b.get("nominal_kwh")
    with db() as con:
        con.execute("INSERT INTO vehicle_settings (vin, nominal_kwh) VALUES (?,?) "
                    "ON CONFLICT(vin) DO UPDATE SET nominal_kwh=excluded.nominal_kwh",
                    (vin, float(kwh) if kwh not in (None, "") else None))
    return {"ok": True}


PHOTOS = Path("/data/photos")


@app.post("/api/vehicle/{vin}/photo")
async def api_photo_upload(vin: str, file: UploadFile = File(...)):
    if not vehicle_row(vin):
        return JSONResponse({"error": "Mașină necunoscută"}, status_code=404)
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        return JSONResponse({"error": "Imaginea e prea mare (max. 8 MB)."}, status_code=400)
    kind = (file.content_type or "").lower()
    if kind not in ("image/jpeg", "image/png", "image/webp"):
        return JSONResponse({"error": "Folosește o imagine JPG, PNG sau WebP."}, status_code=400)
    PHOTOS.mkdir(parents=True, exist_ok=True)
    (PHOTOS / f"{vin}.img").write_bytes(data)
    (PHOTOS / f"{vin}.type").write_text(kind)
    with db() as con:
        con.execute("INSERT INTO vehicle_settings (vin, has_photo) VALUES (?,1) "
                    "ON CONFLICT(vin) DO UPDATE SET has_photo=1", (vin,))
    return {"ok": True}


@app.get("/api/vehicle/{vin}/photo")
async def api_photo(vin: str):
    f, t = PHOTOS / f"{vin}.img", PHOTOS / f"{vin}.type"
    if f.exists():
        return FileResponse(f, media_type=t.read_text() if t.exists() else "image/jpeg",
                            headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "fără poză"}, status_code=404)


@app.post("/api/vehicle/{vin}/wake")
async def api_wake(vin: str):
    row = vehicle_row(vin)
    if not row:
        return JSONResponse({"error": "Mașină necunoscută"}, status_code=404)
    try:
        r = await tesla(row["account_id"], "POST", f"/api/1/vehicles/{vin}/wake_up")
    except (TeslaError, httpx.HTTPError):
        return {"ok": False}
    return {"ok": r.status_code == 200}


# ---------- fișiere statice ----------

@app.get(PUBLIC_KEY_PATH)
async def tesla_public_key():
    f = Path("/data/tesla-public-key.pem")
    if not f.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(f, media_type="application/x-pem-file")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


VENDOR = (STATIC / "vendor").resolve()
VENDOR_TYPES = {".js": "text/javascript", ".css": "text/css", ".png": "image/png"}


@app.get("/vendor/{path:path}")
async def vendor_file(path: str):
    f = (VENDOR / path).resolve()
    if f.is_relative_to(VENDOR) and f.is_file() and f.suffix in VENDOR_TYPES:
        return FileResponse(f, media_type=VENDOR_TYPES[f.suffix],
                            headers={"Cache-Control": "public, max-age=604800"})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/{name}")
async def static_file(name: str):
    if name in STATIC_FILES and name != "login.html":
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "not found"}, status_code=404)
