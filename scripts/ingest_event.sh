#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./scripts/ingest_event.sh \
#     --url http://localhost:8000/api/events/ingest/ \
#     --event-type demo.user.signup \
#     --payload '{"user_id":123,"plan":"pro"}' \
#     --dedupe-key demo-123 \
#     --token "$SCHEDULER_EVENTS_API_TOKEN"
#
# Notes:
# - If --token is omitted, the API requires an authenticated Django session.

URL=""
EVENT_TYPE=""
PAYLOAD_JSON="{}"
DEDUPE_KEY=""
TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      URL="$2"; shift 2 ;;
    --event-type)
      EVENT_TYPE="$2"; shift 2 ;;
    --payload)
      PAYLOAD_JSON="$2"; shift 2 ;;
    --dedupe-key)
      DEDUPE_KEY="$2"; shift 2 ;;
    --token)
      TOKEN="$2"; shift 2 ;;
    -h|--help)
      sed -n '1,40p' "$0"; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$URL" || -z "$EVENT_TYPE" ]]; then
  echo "--url and --event-type are required" >&2
  exit 2
fi

data=$(cat <<JSON
{
  "event_type": ${EVENT_TYPE@Q},
  "payload_json": $PAYLOAD_JSON,
  "dedupe_key": ${DEDUPE_KEY@Q}
}
JSON
)

headers=(
  -H "Content-Type: application/json"
)

if [[ -n "$TOKEN" ]]; then
  headers+=( -H "X-Scheduler-Token: $TOKEN" )
fi

echo "POST $URL" >&2
curl -sS -X POST "$URL" "${headers[@]}" --data "$data" | python -m json.tool
