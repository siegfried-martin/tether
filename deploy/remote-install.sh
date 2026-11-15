#!/usr/bin/env bash
# Runs on the server as root, from deploy.sh. Idempotent.
# All site-specific values (public URL, port) come from /etc/tether/tether.env.
set -euo pipefail
STAGE=$1
APP=/opt/tether
DATA=/var/lib/tether
ETC=/etc/tether
ENV_FILE=$ETC/tether.env

if [ ! -f "$ENV_FILE" ]; then
    echo "missing $ENV_FILE; create it first (see .env.example)" >&2
    exit 1
fi
# Read single values without sourcing the file (it holds secrets with arbitrary characters).
env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
BASE_URL=$(env_value TETHER_BASE_URL)
PORT=$(env_value TETHER_PORT)
DOMAIN=$(printf '%s' "$BASE_URL" | sed -E 's#^https?://##; s#[/:].*$##')
if [ -z "$DOMAIN" ] || ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
    echo "$ENV_FILE needs TETHER_BASE_URL and a numeric TETHER_PORT" >&2
    exit 1
fi
SITE=/etc/nginx/sites-available/$DOMAIN

# Own system user; data and secrets readable only by it (and root).
id tether >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin tether
install -d -o tether -g tether -m 700 "$DATA"
install -d -o root -g tether -m 750 "$ETC"
install -d -m 755 "$APP" "$APP/web"
chown root:tether "$ENV_FILE"; chmod 640 "$ENV_FILE"

# Code: a venv on the system Python, hash-locked dependencies, then the Tether wheel.
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --disable-pip-version-check --require-hashes -r "$STAGE/requirements.txt"
"$APP/.venv/bin/pip" install -q --disable-pip-version-check --no-deps --force-reinstall "$STAGE"/wheel/*.whl
rsync -a --delete "$STAGE/web-dist/" "$APP/web/dist/"
install -m 644 "$STAGE/REVISION" "$APP/REVISION"

# systemd unit (always refreshed from the repo).
install -m 644 "$STAGE/tether.service" /etc/systemd/system/tether.service
systemctl daemon-reload
systemctl enable -q tether
systemctl restart tether

# nginx site: installed once, because certbot edits it in place afterwards.
# Later runs only update the upstream port, so TETHER_PORT stays the one source.
if [ ! -f "$SITE" ]; then
    sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$PORT/g" "$STAGE/nginx-tether.conf" > "$SITE"
    chmod 644 "$SITE"
    ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
    nginx -t -q && systemctl reload nginx
elif ! grep -q "proxy_pass http://127.0.0.1:$PORT;" "$SITE"; then
    sed -i -E "s#proxy_pass http://127\.0\.0\.1:[0-9]+;#proxy_pass http://127.0.0.1:$PORT;#" "$SITE"
    nginx -t -q && systemctl reload nginx
fi

# Health check: the API should answer 401 (sign-in required).
for _ in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/me" || true)
    [ "$code" = 401 ] && { echo "tether $(cat "$APP/REVISION") is up on $DOMAIN (api/me -> 401)"; exit 0; }
    sleep 0.5
done
echo "tether did not come up; journalctl -u tether -n 50" >&2
exit 1
