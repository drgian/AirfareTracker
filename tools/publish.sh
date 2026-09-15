#!/bin/bash
# Publish one site file to the gh-pages branch (which GitHub Pages serves at flightfare.io),
# then wait until the CDN serves the new version.
#   usage: GITHUB_TOKEN=... tools/publish.sh site/index.html index.html "message"
#   the same page file serves both sites: publish to index.html (live) or dev/index.html (test copy)
set -euo pipefail
: "${GITHUB_TOKEN:?set GITHUB_TOKEN to a token with repo access}"
LOCAL=$1; REMOTE=$2; MSG=$3
API="https://api.github.com/repos/drgian/AirfareTracker/contents/$REMOTE"
SHA=$(curl -s -H "Authorization: token $GITHUB_TOKEN" "$API?ref=gh-pages" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sha',''))")
python3 - "$LOCAL" "$SHA" "$MSG" > /tmp/flightfare-publish.json <<'PY'
import base64, json, sys
local, sha, msg = sys.argv[1:4]
d = {"message": msg, "branch": "gh-pages", "content": base64.b64encode(open(local, "rb").read()).decode(),
     "committer": {"name": "drgian", "email": "giandiloreto@gmail.com"}}
if sha: d["sha"] = sha
print(json.dumps(d))
PY
curl -s -X PUT -H "Authorization: token $GITHUB_TOKEN" "$API" -d @/tmp/flightfare-publish.json \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('published', d['commit']['sha'][:12])"
rm -f /tmp/flightfare-publish.json
URL="https://flightfare.io/${REMOTE%index.html}"
WANT=$(shasum -a 256 "$LOCAL" | cut -d' ' -f1)
for i in $(seq 1 60); do
  [ "$(curl -s "$URL?cb=$RANDOM$i" | shasum -a 256 | cut -d' ' -f1)" = "$WANT" ] && { echo "$URL live"; exit 0; }
  sleep 10
done
echo "$URL not live yet"; exit 1
