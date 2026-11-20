#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
# Raise one alert through the halow-alert gateway (POST /alert) with curl.
# WARNING: every wearable on the HaLowLink network vibrates and sounds. Warn people first.
#
#   HALOW_URL=http://<gateway>:8080 HALOW_TOKEN=<token> ./alert.sh ["label"]
#
# Optional: HALOW_FORMAT (raw8, proto or both; default: the gateway's), HALOW_BURST_SECONDS
# (0 to 5), HALOW_TIMEOUT (seconds, default 10). Needs bash and curl 7.55 or newer.
# The token is passed to curl on a file descriptor, not on its command line, so other
# users of this machine cannot read it from the process list.
# Exit code: 0 sent (the gateway's send record is printed), 1 failed, 2 bad settings.
set -euo pipefail

url=${HALOW_URL:-http://127.0.0.1:8080}
url=${url%/}
timeout=${HALOW_TIMEOUT:-10}
die() { echo "alert.sh: $*" >&2; exit 2; }

[[ -n ${HALOW_TOKEN:-} ]] || die "set HALOW_TOKEN to the gateway's token"
[[ $url == http://* || $url == https://* ]] || die "HALOW_URL must start with http:// or https://"
[[ $timeout =~ ^[0-9]+$ ]] || die "HALOW_TIMEOUT must be a whole number of seconds"

json_string() {   # JSON string literal for $1
  local s=$1 out="" c i
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  for (( i = 0; i < ${#s}; i++ )); do
    c=${s:i:1}
    case $c in
      $'\n') out+='\n' ;;
      $'\r') out+='\r' ;;
      $'\t') out+='\t' ;;
      [[:cntrl:]]) ;;                      # drop other control characters
      *) out+=$c ;;
    esac
  done
  printf '"%s"' "$out"
}

body="{\"label\": $(json_string "${1:-curl}")"
if [[ -n ${HALOW_FORMAT:-} ]]; then
  case $HALOW_FORMAT in raw8|proto|both) ;; *) die "HALOW_FORMAT must be raw8, proto or both" ;; esac
  body+=", \"format\": \"$HALOW_FORMAT\""
fi
if [[ -n ${HALOW_BURST_SECONDS:-} ]]; then
  [[ $HALOW_BURST_SECONDS =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "HALOW_BURST_SECONDS must be a number from 0 to 5"
  body+=", \"burst_seconds\": $HALOW_BURST_SECONDS"
  timeout=$((timeout + 5))                # the gateway answers when the burst ends
fi
body+="}"

out=$(mktemp)
trap 'rm -f "$out"' EXIT
code=$(curl -sS --noproxy '*' --connect-timeout 5 --max-time "$timeout" -o "$out" -w '%{http_code}' \
  -X POST "$url/alert" -H 'Content-Type: application/json' \
  -H @<(printf 'Authorization: Bearer %s\n' "$HALOW_TOKEN") \
  --data-binary "$body") || { echo "alert.sh: cannot reach the gateway at $url" >&2; exit 1; }

case $code in
  200) cat "$out"; echo; exit 0 ;;
  401) hint="missing or wrong token: set HALOW_TOKEN to the gateway's token" ;;
  429) hint="rate limited: the gateway sends at most one alert every 0.5 s; retry shortly" ;;
  500) hint="the gateway could not send the alert (HaLowLink unplugged or not set up? see GET /health)" ;;
  503) hint="the gateway cannot send right now (see GET /health)" ;;
  400|413) hint="the gateway rejected the request" ;;
  404) hint="unknown path: is HALOW_URL the halow-alert gateway (http://<host>:8080)?" ;;
  *) hint="unexpected response" ;;
esac
echo "alert.sh: HTTP $code: $hint" >&2
cat "$out" >&2
echo >&2
exit 1
