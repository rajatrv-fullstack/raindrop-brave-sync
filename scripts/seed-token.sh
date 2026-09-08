#!/usr/bin/env bash
# Store the Raindrop API token in the macOS login keychain, without echoing it.
#
#   scripts/seed-token.sh                 # prompt (input hidden)
#   scripts/seed-token.sh --from-env      # take it from $RAINDROP_TOKEN
#
# The keychain is preferred over .env for unattended jobs: a login-keychain item
# with no timeout is readable by a launchd agent without a Touch ID prompt.
set -euo pipefail
SERVICE="raindrop-api"

if [ "${1:-}" = "--from-env" ]; then
  tok="${RAINDROP_TOKEN:?RAINDROP_TOKEN is not set}"
else
  printf 'Raindrop API token (input hidden): ' >&2
  read -rs tok; printf '\n' >&2
fi
[ -n "$tok" ] || { echo "empty token, aborting" >&2; exit 1; }

# -U updates an existing item rather than erroring.
security add-generic-password -U -s "$SERVICE" -a "$USER" -w "$tok"
unset tok
echo "Stored in the login keychain under service '$SERVICE'." >&2
echo "Verify with: scripts/get-token.sh | wc -c   # prints a length, not the token" >&2
