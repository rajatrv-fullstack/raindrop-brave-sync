#!/usr/bin/env bash
# Print the Raindrop API token to stdout for consumption by a pipe.
#
# Resolution order: $RAINDROP_TOKEN -> ~/.raindrop-sync/.env -> macOS keychain.
#
# NEVER run this interactively just to look at the value. A token you have
# displayed is a token you must rotate. Pipe it:
#     scripts/get-token.sh | xargs -I{} curl -sH "Authorization: Bearer {}" ...
set -euo pipefail
ROOT="${RAINDROP_SYNC_ROOT:-$HOME/.raindrop-sync}"

if [ -n "${RAINDROP_TOKEN:-}" ]; then printf '%s' "$RAINDROP_TOKEN"; exit 0; fi

if [ -f "$ROOT/.env" ]; then
  tok="$(grep -E '^RAINDROP_TOKEN=' "$ROOT/.env" | head -1 | cut -d= -f2- | tr -d '"'"'"' \r\n')"
  if [ -n "$tok" ]; then printf '%s' "$tok"; exit 0; fi
fi

if tok="$(security find-generic-password -s raindrop-api -w 2>/dev/null)"; then
  printf '%s' "$tok"; exit 0
fi

echo "No Raindrop token found. Set RAINDROP_TOKEN, populate $ROOT/.env, or run scripts/seed-token.sh" >&2
exit 1
