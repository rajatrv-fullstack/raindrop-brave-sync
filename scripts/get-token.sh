#!/usr/bin/env bash
# Print the Raindrop API token to stdout, for consumption by a pipe. Never displays it.
#
# Resolution order:
#   1. $RAINDROP_TOKEN in the environment
#   2. a non-empty RAINDROP_TOKEN= line in $RAINDROP_SYNC_ROOT/.env (default ~/.raindrop-sync/.env)
#   3. the macOS login keychain, service $RAINDROP_SYNC_KEYCHAIN_SERVICE (default raindrop-api)
#
# A non-empty .env wins over the keychain. If you seeded the keychain and keep getting
# an old token, blank the RAINDROP_TOKEN= line in .env.
#
# RAINDROP_SYNC_NO_KEYCHAIN=1 skips step 3 (tests, CI, Linux): the "security" tool is
# never called.
#
# NEVER run this interactively just to look at the value. A token you have displayed is
# a token you must rotate. Never put it in argv either (ps shows it, so does the shell
# history). Feed curl a config file on stdin instead:
#     printf 'header = "Authorization: Bearer %s"\n' "$(~/.raindrop-sync/bin/get-token.sh)" \
#       | curl -s --config - https://api.raindrop.io/rest/v1/user
set -euo pipefail
ROOT="${RAINDROP_SYNC_ROOT:-$HOME/.raindrop-sync}"
SERVICE="${RAINDROP_SYNC_KEYCHAIN_SERVICE:-raindrop-api}"
NO_KEYCHAIN="${RAINDROP_SYNC_NO_KEYCHAIN:-0}"

if [ -n "${RAINDROP_TOKEN:-}" ]; then printf '%s' "$RAINDROP_TOKEN"; exit 0; fi

if [ -f "$ROOT/.env" ]; then
  tok="$(grep -E '^RAINDROP_TOKEN=' "$ROOT/.env" | head -1 | cut -d= -f2- | tr -d '"'"'"' \r\n')"
  if [ -n "$tok" ]; then printf '%s' "$tok"; exit 0; fi
fi

if [ "$NO_KEYCHAIN" != "1" ]; then
  if tok="$(security find-generic-password -s "$SERVICE" -w 2>/dev/null)"; then
    printf '%s' "$tok"; exit 0
  fi
fi

echo "No Raindrop token found. Set RAINDROP_TOKEN, put it in $ROOT/.env, or run $ROOT/bin/seed-token.sh (keychain service '$SERVICE')" >&2
exit 1
