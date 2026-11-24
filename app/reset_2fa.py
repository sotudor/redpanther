"""Dezactivează autentificarea în doi pași, pentru cazul în care ai pierdut și telefonul,
și codurile de rezervă. Se rulează pe server, din folderul aplicației:

    docker compose exec app python reset_2fa.py

După asta intri doar cu parola și poți activa 2FA din nou din Setări.
"""
import sqlite3

import twofa

con = sqlite3.connect("/data/app.db")
with con:
    twofa.init_db(con)
    was = twofa.enabled(con)
    twofa.disable(con)
con.close()
print("Autentificarea în doi pași a fost dezactivată." if was else "Autentificarea în doi pași nu era activă.")
