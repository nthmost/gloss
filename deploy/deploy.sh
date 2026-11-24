#!/usr/bin/env bash
# Deploy the gloss web app to the serve host (zephyr) behind Apache.
# Run from a dev machine after pushing to GitHub:  deploy/deploy.sh
#
# What it does (idempotent):
#   1. DNS: ensure <sub>.<domain> A record exists at Gandi LiveDNS (needs GANDI_KEY_FILE)
#   2. git clone/pull the repo into /opt/gloss/app, build its venv
#   3. install the systemd unit (port 8902) and (re)start it
#   4. Apache: bootstrap http vhost → certbot webroot → final http+https vhosts
#
# The database is NOT deployed here; deploy/refresh.sh on the build host ships it.
# Until the first refresh lands, the app answers 503 on /api/*.

set -euo pipefail

HOST=${HOST:-zephyr}
DOMAIN=${DOMAIN:-gloss.nthmost.com}
REPO=${REPO:-https://github.com/nthmost/gloss.git}
BRANCH=${BRANCH:-main}
APP=/opt/gloss/app
GANDI_KEY_FILE=${GANDI_KEY_FILE:-$HOME/projects/nthmost-systems/.secrets/gandi-apikey}
ZONE=${DOMAIN#*.}          # nthmost.com
SUB=${DOMAIN%%.*}          # gloss

here=$(cd "$(dirname "$0")" && pwd)

# ---------------------------------------------------------------- 1. DNS
if [ -f "$GANDI_KEY_FILE" ]; then
  KEY=$(tr -d '[:space:]' < "$GANDI_KEY_FILE")
  ip=$(ssh "$HOST" "curl -4 -s https://ifconfig.me")
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" \
    "https://api.gandi.net/v5/livedns/domains/$ZONE/records/$SUB/A")
  if [ "$code" = "404" ]; then
    echo "==> DNS: creating $DOMAIN A $ip"
    curl -sS -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
      -d "{\"rrset_values\":[\"$ip\"],\"rrset_ttl\":1800}" \
      "https://api.gandi.net/v5/livedns/domains/$ZONE/records/$SUB/A" && echo
  else
    echo "==> DNS: $DOMAIN record already exists (HTTP $code)"
  fi
else
  echo "==> DNS: no Gandi key at $GANDI_KEY_FILE — make sure $DOMAIN resolves to $HOST yourself"
fi

# ---------------------------------------------------------------- 2. code
echo "==> Code: $REPO@$BRANCH → $HOST:$APP"
ssh "$HOST" bash -s <<EOF
set -e
sudo mkdir -p /opt/gloss/data && sudo chown -R \$USER:\$USER /opt/gloss
if [ -d $APP/.git ]; then git -C $APP fetch -q origin && git -C $APP reset -q --hard origin/$BRANCH
else git clone -q -b $BRANCH $REPO $APP; fi
cd $APP
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -e .
echo "    $(git -C $APP log -1 --format='%h %s')"
EOF

# ---------------------------------------------------------------- 3. service
echo "==> Service: gloss.service"
scp -q "$here/gloss.service" "$HOST:/tmp/gloss.service"
ssh "$HOST" "sudo mv /tmp/gloss.service /etc/systemd/system/gloss.service && sudo systemctl daemon-reload && sudo systemctl enable -q gloss && sudo systemctl restart gloss && sleep 1 && systemctl is-active gloss"

# ---------------------------------------------------------------- 4. apache + tls
have_cert=$(ssh "$HOST" "[ -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ] && echo yes || echo no")
if [ "$have_cert" = "no" ]; then
  echo "==> TLS: no cert yet — bootstrap vhost + certbot webroot"
  scp -q "$here/apache/gloss-bootstrap.conf" "$HOST:/tmp/$DOMAIN.conf"
  ssh "$HOST" "sudo mv /tmp/$DOMAIN.conf /etc/apache2/sites-available/$DOMAIN.conf && sudo a2enmod -q proxy proxy_http headers rewrite >/dev/null && sudo a2ensite -q $DOMAIN && sudo apache2ctl configtest && sudo systemctl reload apache2"
  echo "    waiting for DNS to resolve $DOMAIN ..."
  for i in $(seq 1 30); do
    if [ -n "$(dig +short "$DOMAIN" @1.1.1.1)" ]; then break; fi; sleep 10
  done
  ssh "$HOST" "sudo mkdir -p /var/www/html/.well-known/acme-challenge && sudo certbot certonly --webroot -w /var/www/html -d $DOMAIN --non-interactive --agree-tos --keep-until-expiring"
fi

echo "==> Apache: final vhosts"
scp -q "$here/apache/gloss.nthmost.com.conf" "$HOST:/tmp/$DOMAIN.conf"
scp -q "$here/apache/gloss.nthmost.com-le-ssl.conf" "$HOST:/tmp/$DOMAIN-le-ssl.conf"
ssh "$HOST" "sudo mv /tmp/$DOMAIN.conf /etc/apache2/sites-available/$DOMAIN.conf && sudo mv /tmp/$DOMAIN-le-ssl.conf /etc/apache2/sites-available/$DOMAIN-le-ssl.conf && sudo a2ensite -q $DOMAIN $DOMAIN-le-ssl && sudo apache2ctl configtest && sudo systemctl reload apache2"

echo "==> Done: https://$DOMAIN"
