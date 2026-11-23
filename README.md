# RedPanther

O aplicație personală, găzduită de tine, pentru mașinile Tesla: drumuri cu hartă, încărcări cu grafice, pierderi în parcare, statistici și sănătatea bateriei. Datele vin direct de la mașină prin **Fleet Telemetry**, API-ul oficial Tesla, pe propriul tău server.

> *A self-hosted Tesla companion app (drives, charging, parking losses, stats, battery health) built on the official Tesla Fleet API and Fleet Telemetry. Documentation is in Romanian.*

> **Proiect neoficial.** Nu e afiliat, susținut sau aprobat de Tesla, Inc. „Tesla” e o marcă înregistrată a proprietarului ei.

## Ce face

- **Car**: bateria și autonomia, limita de încărcare, sănătatea estimată a bateriei (din energia reală în kWh), temperatura bateriei, echilibrul celulelor, presiunea în roți, temperaturile motoarelor și poza mașinii, încărcată de tine.
- **Drives**: fiecare drum, cu traseul pe hartă, distanța, durata, energia, consumul, autonomia folosită și viteza.
- **Charging**: fiecare încărcare, cu graficele puterii, temperaturii bateriei și nivelului bateriei, eficiența încărcării și costul (acasă 0, Supercharger din facturile Tesla, alte rețele introduse manual).
- **Parked**: energia pierdută cât mașina stă parcată, inclusiv cu Sentry.
- **Stats**: totaluri și grafice pe 7, 30, 90 de zile sau pe un interval ales.
- **Mai multe mașini și conturi Tesla** în aceeași aplicație.
- Se instalează pe telefon ca aplicație (PWA), protejată cu parolă.

## Cum funcționează

```
Mașina ──(Fleet Telemetry, mTLS, port 4443)──► serverul tău ──► baza de date SQLite
                                                   │
Telefonul (PWA) ◄────────(HTTPS, port 443)─────────┘
                                                   │
Tesla Fleet API ◄── login OAuth, lista mașinilor, configurarea telemetriei (semnată de proxy-ul oficial Tesla)
```

Serviciile (Docker Compose):

- `app`: aplicația (Python / FastAPI) și receptorul de telemetrie ([python-fleet-telemetry](https://github.com/Teslemetry/python-fleet-telemetry));
- `proxy`: [proxy-ul oficial Tesla](https://github.com/teslamotors/vehicle-command), care semnează configurația trimisă mașinilor;
- `caddy`: HTTPS automat cu Let's Encrypt.

Rulează și pe un server foarte mic (de exemplu Oracle Cloud Always Free, 1 GB RAM, cu swap).

## De ce ai nevoie

- un cont Tesla cu mașina, cu software suficient de nou pentru Fleet Telemetry;
- un cont pe [developer.tesla.com](https://developer.tesla.com), cu o aplicație înregistrată;
- un server Linux cu Docker și o adresă publică, cu porturile **80**, **443** și **4443** deschise;
- un nume de domeniu (merge și unul gratuit, de exemplu de la DuckDNS).

## Instalare

### 1. Cheile aplicației Tesla

Pe calculatorul tău:

```bash
openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
openssl ec -in private-key.pem -pubout -out public-key.pem
```

Păstrează `private-key.pem` în siguranță și nu-l publica.

### 2. Aplicația pe developer.tesla.com

- **Origine permisă:** `https://domeniul-tau`
- **Adresă de redirecționare:** `https://domeniul-tau/auth/callback`
- **Permisiuni:** datele vehiculului, locația vehiculului, comenzi și comenzi de încărcare.

Notează **Client ID** și **Client Secret**. Setează și o limită de facturare la Fleet API, ca să nu ai surprize.

### 3. Serverul

```bash
git clone https://github.com/sotudor/redpanther.git
cd redpanther
bash setup.sh            # domeniul, Client ID, Client Secret, parola aplicației
mkdir -p data
```

Copiază cheile în `data/` (de pe calculatorul tău, cu `scp`):

- `public-key.pem` → `data/tesla-public-key.pem`
- `private-key.pem` → `data/tesla-private-key.pem`

Proxy-ul Tesla rulează ca utilizatorul cu UID 1000, deci:

```bash
sudo chown 1000:1000 data/tesla-private-key.pem
chmod 600 data/tesla-private-key.pem
```

Pornește totul:

```bash
docker compose up -d --build
```

Verifică faptul că cheia publică se vede la
`https://domeniul-tau/.well-known/appspecific/com.tesla.3p.public-key.pem`.

### 4. Înregistrarea domeniului la Tesla

O singură dată, cu un token de partener:

```bash
TOKEN=$(curl -s -X POST https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token \
  -d grant_type=client_credentials -d client_id=CLIENT_ID \
  --data-urlencode client_secret='CLIENT_SECRET' \
  -d 'scope=openid vehicle_device_data vehicle_cmds vehicle_charging_cmds' \
  -d audience=https://fleet-api.prd.eu.vn.cloud.tesla.com | sed -E 's/.*"access_token":"([^"]+)".*/\1/')

curl -X POST https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/partner_accounts \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"domain":"domeniul-tau"}'
```

În afara Europei, folosește regiunea Fleet API potrivită contului tău.

### 5. Mașina

1. Deschide aplicația pe telefon, la `https://domeniul-tau`, intră cu parola și conectează contul Tesla.
2. **Cheia virtuală:** lângă mașină, cu ea selectată în aplicația Tesla, deschide `https://www.tesla.com/_ak/domeniul-tau` și acceptă.
3. În aplicație: **profil → Setări → Activează telemetria**, apoi **Verifică la Tesla** până apare „Sincronizată: Da”.
4. Repetă pentru fiecare mașină.

Pe Android, din meniul Chrome: **Instalează aplicația**.

## Costuri

- **Server:** de la 0 (Oracle Always Free) la câțiva euro pe lună.
- **Tesla Fleet API:** facturat pe utilizare, cu un credit lunar gratuit. Telemetria trimite doar semnalele care se schimbă, iar intervalele din `app/telemetry.py` sunt alese ca să rămână ieftine. Urmărește consumul pe pagina de facturare de pe developer.tesla.com.

## Securitate

- Nu publica niciodată `.env`, folderul `data/` sau cheia privată. Fișierul `.gitignore` le exclude.
- Aplicația e protejată cu parolă. Tokenurile Tesla rămân doar pe serverul tău.
- Dacă un secret a ajuns vreodată public, regenerează-l.

## Licență

[MIT](LICENSE)
