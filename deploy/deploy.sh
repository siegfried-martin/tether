#!/usr/bin/env bash
# Build Tether locally and install it on the server. Safe to rerun.
#   deploy/deploy.sh
# The target comes from TETHER_DEPLOY_HOST (an ssh host or alias), set in the
# environment or in deploy/.env (git-ignored; see deploy/.env.example).
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${TETHER_DEPLOY_HOST:-}
if [ -z "$HOST" ] && [ -f deploy/.env ]; then HOST=$(sed -n 's/^TETHER_DEPLOY_HOST=//p' deploy/.env | tail -n 1); fi
if [ -z "$HOST" ]; then
    echo "set TETHER_DEPLOY_HOST in deploy/.env (see deploy/.env.example)" >&2
    exit 1
fi

# Deploy only committed code, so the server always runs a known revision.
if [ -n "$(git status --porcelain)" ] && [ "${TETHER_DEPLOY_DIRTY:-}" != 1 ]; then
    echo "uncommitted changes; commit first (or TETHER_DEPLOY_DIRTY=1 to override)" >&2
    exit 1
fi
REV=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain)" ]; then REV="$REV-dirty"; fi

echo "==> build $REV"
uv export -q --frozen --no-dev --no-emit-project --format requirements-txt -o deploy/requirements.txt
rm -rf build/wheel && uv build -q --wheel -o build/wheel
(cd web && npm ci --silent && npm run build --silent)

echo "==> upload"
STAGE=$(ssh "$HOST" mktemp -d /tmp/tether-release.XXXXXX)
rsync -a build/wheel/ "$HOST:$STAGE/wheel/"
rsync -a web/dist/ "$HOST:$STAGE/web-dist/"
echo "$REV" > build/REVISION
rsync -a deploy/requirements.txt deploy/remote-install.sh deploy/tether.service deploy/nginx-tether.conf build/REVISION "$HOST:$STAGE/"

echo "==> install"
ssh "$HOST" "sudo bash $STAGE/remote-install.sh $STAGE && rm -rf $STAGE"
