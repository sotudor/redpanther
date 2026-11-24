"""Autentificarea în doi pași cu coduri TOTP (Google Authenticator și aplicațiile compatibile)
și coduri de rezervă de unică folosință."""
import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote, urlencode

ISSUER = "RedPanther"
STEP = 30                 # un cod nou la 30 de secunde (RFC 6238)
DIGITS = 6
DRIFT = 1                 # acceptă și codul de dinainte și de după, pentru ceasuri ușor decalate
SETUP_MINUTES = 15        # cât timp e valabil un cod QR afișat la activare
RECOVERY_COUNT = 10
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # fără 0/o, 1/l/i, ușor de copiat de mână


def init_db(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS twofa (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            secret TEXT, pending TEXT, pending_ts REAL,
            last_step INTEGER NOT NULL DEFAULT 0, enabled_ts REAL
        );
        CREATE TABLE IF NOT EXISTS twofa_recovery (hash TEXT PRIMARY KEY, used_ts REAL);
        INSERT OR IGNORE INTO twofa (id, last_step) VALUES (1, 0);
        """
    )


# ---------- TOTP ----------

def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS
    return f"{code:0{DIGITS}d}"


def _match(secret: str, code: str, now: float, after_step: int = 0):
    """Pasul de timp al codului dacă e valid și mai nou decât `after_step`, altfel None.
    Un cod folosit o dată nu mai e acceptat, nici în aceeași fereastră de 30 de secunde."""
    if len(code) != DIGITS or not code.isdigit():
        return None
    base = int(now // STEP)
    for d in range(-DRIFT, DRIFT + 1):
        step = base + d
        if step > after_step and hmac.compare_digest(_hotp(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str) -> str:
    label = quote(f"{ISSUER}:{account}", safe=":")
    q = urlencode({"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{q}"


# ---------- starea ----------

def _row(con):
    return con.execute("SELECT secret, pending, pending_ts, last_step, enabled_ts FROM twofa WHERE id=1").fetchone()


def enabled(con) -> bool:
    return bool(_row(con)[0])


def status(con) -> dict:
    r = _row(con)
    left = con.execute("SELECT count(*) FROM twofa_recovery WHERE used_ts IS NULL").fetchone()[0]
    return {"enabled": bool(r[0]), "enabled_ts": r[4], "recovery_left": left if r[0] else 0}


def _normalize(code: str) -> str:
    return "".join(c for c in (code or "").lower() if c.isalnum())


def _hash(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


def new_recovery(con) -> list[str]:
    """Generează coduri de rezervă noi (cele vechi nu mai merg). Se afișează o singură dată."""
    con.execute("DELETE FROM twofa_recovery")
    codes = []
    for _ in range(RECOVERY_COUNT):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
        con.execute("INSERT INTO twofa_recovery VALUES (?, NULL)", (_hash(raw),))
    return codes


def verify(con, code: str, now: float, allow_recovery: bool = True):
    """Verifică un cod din aplicație sau unul de rezervă. Întoarce "totp", "recovery" sau None."""
    r = _row(con)
    if not r[0]:
        return None
    norm = _normalize(code)
    step = _match(r[0], norm, now, r[3])
    if step is not None:
        con.execute("UPDATE twofa SET last_step=? WHERE id=1", (step,))
        return "totp"
    if allow_recovery and len(norm) == 10:
        cur = con.execute("UPDATE twofa_recovery SET used_ts=? WHERE hash=? AND used_ts IS NULL", (now, _hash(norm)))
        if cur.rowcount == 1:
            return "recovery"
    return None


def start_setup(con, now: float) -> str:
    secret = new_secret()
    con.execute("UPDATE twofa SET pending=?, pending_ts=? WHERE id=1", (secret, now))
    return secret


def confirm_setup(con, code: str, now: float):
    """Activează 2FA dacă codul corespunde secretului afișat. Întoarce codurile de rezervă sau None."""
    r = _row(con)
    if not r[1] or now - (r[2] or 0) > SETUP_MINUTES * 60:
        return None
    step = _match(r[1], _normalize(code), now)
    if step is None:
        return None
    con.execute("UPDATE twofa SET secret=pending, pending=NULL, pending_ts=NULL, last_step=?, enabled_ts=? WHERE id=1",
                (step, now))
    return new_recovery(con)


def setup_expired(con, now: float) -> bool:
    r = _row(con)
    return not r[1] or now - (r[2] or 0) > SETUP_MINUTES * 60


def disable(con):
    con.execute("UPDATE twofa SET secret=NULL, pending=NULL, pending_ts=NULL, last_step=0, enabled_ts=NULL WHERE id=1")
    con.execute("DELETE FROM twofa_recovery")
