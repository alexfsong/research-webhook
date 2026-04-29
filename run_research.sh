#!/bin/bash
# Trigger research from the server itself.
# Usage: ./run_research.sh "your query here"
set -euo pipefail
if [ $# -lt 1 ]; then
  echo "usage: $0 \"<query>\"" >&2
  exit 1
fi
QUERY="$*"
AUTH_HEADER=()
if [ -n "${WEBHOOK_API_KEY:-}" ]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${WEBHOOK_API_KEY}")
fi
curl -sS -X POST http://localhost:8000/research \
  -H "Content-Type: application/json" \
  "${AUTH_HEADER[@]}" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"query": sys.argv[1]}))' "$QUERY")" \
  | python3 -m json.tool
