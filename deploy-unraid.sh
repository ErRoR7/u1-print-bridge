#!/bin/bash
# Deploy a pushed U1 print-bridge version to Tower.
#
# Usage: deploy-unraid.sh <version>   (e.g. deploy-unraid.sh 1.0.19)
#
# Flow (Joel's standing instruction, Nov 7 2026):
#   1. Wait for the GitHub Actions docker-publish workflow to push the new
#      version tag to GHCR for BOTH images (api + slicer). Polls the GHCR
#      tags API (anonymous, public packages) up to 20 minutes.
#   2. Have Unraid check for Docker updates (dockerupdate script).
#   3. Update both containers via dockerMan's own update_container script
#      (pulls from the template Repository, recreates from the template with
#      the dockerman label, cleans up the old image).
#   4. Verify the bridge health endpoint reports the new version.
#
# Run from the Muse VM. Requires the ssh_tower.sh wrapper and Tower
# reachable over Tailscale. Never prints secrets.
set -euo pipefail

VERSION="${1:?usage: deploy-unraid.sh <version>}"
SSH=~/workspace/ssh-tower/ssh_tower.sh
IMAGES="u1-print-bridge-api u1-print-bridge-slicer"

ghcr_tags() {
  local img="$1"
  local token
  token=$(curl -s "https://ghcr.io/token?service=ghcr.io&scope=repository:error7/${img}:pull" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
  [ -n "$token" ] || return 1
  curl -s -H "Authorization: Bearer $token" "https://ghcr.io/v2/error7/${img}/tags/list" \
    | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin).get('tags') or []))" 2>/dev/null
}

echo "Waiting for GHCR tag $VERSION on both images (up to 20 min)..."
for i in $(seq 1 40); do
  ok=1
  for img in $IMAGES; do
    tags=$(ghcr_tags "$img" || echo "")
    case " $tags " in
      *" $VERSION "*) echo "  $img: $VERSION present" ;;
      *) echo "  $img: $VERSION not yet (have: $tags)"; ok=0 ;;
    esac
  done
  [ "$ok" = 1 ] && break
  [ "$i" = 40 ] && { echo "TIMEOUT waiting for GHCR tag $VERSION"; exit 1; }
  sleep 30
done

echo "Triggering Unraid Docker update check..."
"$SSH" 'php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/dockerupdate' 2>&1 | tail -2

echo "Updating u1-print-bridge-api..."
"$SSH" 'php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container u1-print-bridge-api' 2>&1 | tail -3

echo "Updating u1-print-bridge-slicer..."
"$SSH" 'php /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container u1-print-bridge-slicer' 2>&1 | tail -3

echo "Verifying bridge health..."
sleep 10
"$SSH" 'curl -sf --max-time 5 http://localhost:8080/api/health' 2>&1 | head -c 600
echo ""
echo "Done. Confirm the health above shows version $VERSION."
