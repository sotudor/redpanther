#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
  read -p "Fisierul .env exista deja. Il suprascriu? (d/N) " r
  [ "$r" = "d" ] || exit 0
fi

read -p "Domeniul (ex: redpanther.duckdns.org): " DOMAIN
read -p "Tesla Client ID: " CID
read -s -p "Tesla Client Secret (nu se afiseaza cand scrii): " CSEC; echo
read -s -p "Alege o parola pentru aplicatie (nu se afiseaza): " PASS; echo
read -p "Autonomia masinii noi in km (optional, Enter ca sa sari): " ORIG

case "$CSEC$PASS" in
  *\'*) echo "Secretul sau parola nu pot contine apostrof ('). Reia scriptul."; exit 1;;
esac

SS=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')

cat > .env <<ENVEOF
DOMAIN='$DOMAIN'
BASE_URL='https://$DOMAIN'
TESLA_CLIENT_ID='$CID'
TESLA_CLIENT_SECRET='$CSEC'
APP_PASSWORD='$PASS'
SESSION_SECRET='$SS'
ORIGINAL_RANGE_KM='$ORIG'
ENVEOF
chmod 600 .env
mkdir -p data
echo ""
echo "Gata. Porneste aplicatia cu:  docker compose up -d --build"
