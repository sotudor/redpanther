"""Recepția datelor Fleet Telemetry trimise direct de mașini și configurarea lor."""
import datetime
import json
import os
import ssl
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from aiohttp import web as _aioweb
from fleet_telemetry import ServerCredentials, TelemetryServer


class _NoCompressWebSocket(_aioweb.WebSocketResponse):
    """Mașinile Tesla resping compresia WebSocket negociată implicit de aiohttp
    („invalid compression negotiation”), așa că o dezactivăm."""

    def __init__(self, *args, **kwargs):
        kwargs["compress"] = False
        super().__init__(*args, **kwargs)


_aioweb.WebSocketResponse = _NoCompressWebSocket

PORT = int(os.environ.get("TELEMETRY_PORT") or 4443)
HOSTNAME = urlparse(os.environ["BASE_URL"]).hostname
CERT_DIR = Path("/data/telemetry_certs")
PROXY_DIR = Path("/data/proxy")
PROXY_URL = os.environ.get("TESLA_PROXY_URL", "https://proxy:4443")
CONFIG_DAYS = 330
LOG_KEEP_DAYS = 400

# Semnalele cerute și la câte secunde, cel mult, le trimite mașina.
# Mașina trimite un semnal doar când i se schimbă valoarea, deci cât stă
# parcată și nu se întâmplă nimic, nu se trimite (și nu se plătește) aproape nimic.
FIELDS = {
    # condus
    "Gear": 1,
    "VehicleSpeed": 10,
    "Odometer": 60,
    "Location": 15,
    # baterie
    "EnergyRemaining": 60,
    "Soc": 60,
    "BatteryLevel": 120,
    "RatedRange": 120,
    "ModuleTempMax": 60,
    "ModuleTempMin": 60,
    "BrickVoltageMax": 300,
    "BrickVoltageMin": 300,
    "PackVoltage": 120,
    "PackCurrent": 60,
    "BatteryHeaterOn": 30,
    "BMSState": 120,
    "IsolationResistance": 1800,
    "LifetimeEnergyUsed": 600,
    # încărcare
    "DetailedChargeState": 1,
    "ChargeLimitSoc": 300,
    "ACChargingPower": 30,
    "DCChargingPower": 15,
    "ACChargingEnergyIn": 60,
    "DCChargingEnergyIn": 30,
    "FastChargerPresent": 10,
    # stare și climă
    "InsideTemp": 300,
    "OutsideTemp": 300,
    "Locked": 30,
    "SentryMode": 30,
    "TpmsPressureFl": 600,
    "TpmsPressureFr": 600,
    "TpmsPressureRl": 600,
    "TpmsPressureRr": 600,
    # motoare
    "DiStatorTempR": 120,
    "DiHeatsinkTR": 120,
    "DiStatorTempF": 120,
    "DiHeatsinkTF": 120,
}
LOCATION_FIELDS = {"Location"}

_server: TelemetryServer | None = None
_creds: ServerCredentials | None = None


def _extract(record) -> dict:
    """Valorile unei înregistrări, cu enum-urile transformate în nume (ex. ShiftStateD)."""
    from fleet_telemetry.proto import vehicle_data_pb2 as vd
    out = {}
    for datum in record.message.data:
        try:
            name = vd.Field.Name(datum.key)
        except ValueError:
            continue
        which = datum.value.WhichOneof("value")
        if which is None or which == "invalid":
            continue
        raw = getattr(datum.value, which)
        fd = datum.value.DESCRIPTOR.fields_by_name[which]
        if fd.enum_type is not None:
            ev = fd.enum_type.values_by_number.get(raw)
            out[name] = ev.name if ev else str(raw)
        else:
            out[name] = raw
    return out


def _plain(value):
    """Transformă o valoare protobuf în ceva ce se poate salva ca JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "latitude") and hasattr(value, "longitude"):
        return {"lat": value.latitude, "lon": value.longitude}
    return str(value)


def _ensure_proxy_tls():
    """Certificat intern pentru proxy-ul Tesla (folosit doar în rețeaua Docker)."""
    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    cert_p, key_p = PROXY_DIR / "tls-cert.pem", PROXY_DIR / "tls-key.pem"
    if not (cert_p.exists() and key_p.exists()):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "proxy")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("proxy")]), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
        cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    # Proxy-ul rulează ca utilizatorul 1000 (ubuntu), deci trebuie să poată citi fișierele.
    for p in (PROXY_DIR, cert_p, key_p):
        try:
            os.chown(p, 1000, 1000)
        except OSError:
            pass
    os.chmod(key_p, 0o600)


def init_db(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS telemetry_latest (
            vin TEXT, field TEXT, value TEXT, ts REAL, PRIMARY KEY (vin, field)
        );
        CREATE TABLE IF NOT EXISTS telemetry_log (
            vin TEXT, ts REAL, field TEXT, value REAL
        );
        CREATE INDEX IF NOT EXISTS telemetry_log_idx ON telemetry_log(vin, field, ts);
        CREATE TABLE IF NOT EXISTS telemetry_events (
            vin TEXT, ts REAL, field TEXT, value TEXT
        );
        CREATE INDEX IF NOT EXISTS telemetry_events_idx ON telemetry_events(vin, field, ts);
        CREATE TABLE IF NOT EXISTS telemetry_loc (
            vin TEXT, ts REAL, lat REAL, lon REAL
        );
        CREATE INDEX IF NOT EXISTS telemetry_loc_idx ON telemetry_loc(vin, ts);
        CREATE TABLE IF NOT EXISTS telemetry_conn (
            vin TEXT PRIMARY KEY, connected INTEGER, ts REAL
        );
        """
    )


async def start(db):
    """Pornește serverul de telemetrie. `db` e funcția care deschide baza de date."""
    global _server, _creds
    with db() as con:
        init_db(con)
    _ensure_proxy_tls()
    _creds = ServerCredentials(str(CERT_DIR))
    _creds.ensure(HOSTNAME)

    def on_data(record):
        now = time.time()
        try:
            fields = _extract(record)
        except Exception as e:
            print("telemetry: nu am putut citi înregistrarea:", repr(e), flush=True)
            return
        with db() as con:
            for name, value in fields.items():
                v = _plain(value)
                con.execute(
                    "INSERT INTO telemetry_latest VALUES (?,?,?,?) "
                    "ON CONFLICT(vin, field) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                    (record.vin, name, json.dumps(v), now),
                )
                if isinstance(v, bool):
                    con.execute("INSERT INTO telemetry_log VALUES (?,?,?,?)",
                                (record.vin, now, name, 1.0 if v else 0.0))
                elif isinstance(v, (int, float)):
                    con.execute("INSERT INTO telemetry_log VALUES (?,?,?,?)",
                                (record.vin, now, name, float(v)))
                elif isinstance(v, str):
                    con.execute("INSERT INTO telemetry_events VALUES (?,?,?,?)",
                                (record.vin, now, name, v))
                elif isinstance(v, dict) and "lat" in v:
                    con.execute("INSERT INTO telemetry_loc VALUES (?,?,?,?)",
                                (record.vin, now, v["lat"], v["lon"]))
            con.execute(
                "INSERT INTO telemetry_conn VALUES (?,1,?) "
                "ON CONFLICT(vin) DO UPDATE SET connected=1, ts=excluded.ts",
                (record.vin, now),
            )

    def on_connectivity(record):
        connected = 1 if (_server is not None and _server.is_connected(record.vin)) else 0
        print(f"telemetry: {record.vin} {'conectată' if connected else 'deconectată'}", flush=True)
        with db() as con:
            con.execute(
                "INSERT INTO telemetry_conn VALUES (?,?,?) "
                "ON CONFLICT(vin) DO UPDATE SET connected=excluded.connected, ts=excluded.ts",
                (record.vin, connected, time.time()),
            )

    _server = TelemetryServer(ssl_context=_creds.build_ssl_context(), port=PORT)
    _server.on_data(on_data)
    _server.on_connectivity(on_connectivity)
    await _server.start()
    print(f"telemetry: ascult pe portul {PORT} pentru {HOSTNAME}", flush=True)


def prune(db):
    with db() as con:
        limit = time.time() - LOG_KEEP_DAYS * 86400
        for t in ("telemetry_log", "telemetry_events", "telemetry_loc"):
            con.execute(f"DELETE FROM {t} WHERE ts < ?", (limit,))


def config_body(vin: str, with_location: bool = True) -> dict:
    fields = {k: v for k, v in FIELDS.items() if with_location or k not in LOCATION_FIELDS}
    return {
        "vins": [vin],
        "config": {
            "hostname": HOSTNAME,
            "port": PORT,
            "ca": _creds.ca_certificate_pem if _creds else "",
            "fields": {k: {"interval_seconds": v} for k, v in fields.items()},
            "prefer_typed": True,
            "exp": int(time.time()) + CONFIG_DAYS * 86400,
        },
    }


async def push_config(vin: str, token: str, with_location: bool = True) -> tuple[int, dict]:
    """Trimite configurația, semnată de proxy-ul oficial Tesla cu cheia ta privată."""
    async with httpx.AsyncClient(timeout=60, verify=False) as c:
        r = await c.post(f"{PROXY_URL}/api/1/vehicles/fleet_telemetry_config",
                         headers={"Authorization": f"Bearer {token}"},
                         json=config_body(vin, with_location))
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": r.text[:300]}


def live(db, vin: str) -> dict:
    with db() as con:
        rows = con.execute("SELECT field, value, ts FROM telemetry_latest WHERE vin=?", (vin,)).fetchall()
        conn = con.execute("SELECT connected, ts FROM telemetry_conn WHERE vin=?", (vin,)).fetchone()
        hist = {f: history(con, vin, f, h) for f, h in HISTORY_HOURS.items()}
    values = {r["field"]: {"value": json.loads(r["value"]), "ts": r["ts"]} for r in rows}
    return {
        "connected": bool(conn["connected"]) if conn else False,
        "last_seen": conn["ts"] if conn else None,
        "values": values,
        "history": hist,
    }


# Câte ore din urmă se trimit pentru graficele din tab-ul Car: cea mai lungă fereastră
# dintre cele care se pot alege în aplicație (temperatura 2/6/12 h, energia 6/12/24 h).
HISTORY_HOURS = {"ModuleTempMax": 12, "EnergyRemaining": 24}


def history(con, vin: str, field: str, hours: float) -> list:
    """Evoluția unui semnal în ultimele ore. Mașina trimite valoarea doar când se schimbă,
    așa că seria începe cu ultima valoare de dinaintea intervalului și se termină acum
    cu ultima valoare cunoscută."""
    now = time.time()
    since = now - hours * 3600
    prev = con.execute(
        "SELECT value FROM telemetry_log WHERE vin=? AND field=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (vin, field, since),
    ).fetchone()
    pts = [[since, prev[0]]] if prev else []
    pts += [[r[0], r[1]] for r in con.execute(
        "SELECT ts, value FROM telemetry_log WHERE vin=? AND field=? AND ts>? ORDER BY ts",
        (vin, field, since),
    )]
    if pts:
        pts.append([now, pts[-1][1]])
    return pts
