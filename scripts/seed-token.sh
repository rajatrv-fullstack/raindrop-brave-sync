#!/usr/bin/env bash
# Store the Raindrop API token in the macOS login keychain, without echoing it.
#
#   seed-token.sh                 # prompt (input hidden)
#   seed-token.sh --from-env      # take it from $RAINDROP_TOKEN
#   seed-token.sh --dry-run       # validate the input and say where it WOULD go; writes nothing
#
# The item is written under service $RAINDROP_SYNC_KEYCHAIN_SERVICE (default raindrop-api,
# the same name get-token.sh reads) with "security ... -U", which REPLACES an existing item
# under that service. Re-running with a wrong value overwrites a good one, so check with
# --dry-run first if in doubt. Remember that get-token.sh prefers a non-empty .env over the
# keychain.
#
# The keychain is preferred over .env for unattended jobs: a login-keychain item
# with no timeout is readable by a launchd agent without a Touch ID prompt.
set -euo pipefail
SERVICE="${RAINDROP_SYNC_KEYCHAIN_SERVICE:-raindrop-api}"
ACCOUNT="${USER:-$(id -un)}"
FROM_ENV=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --from-env) FROM_ENV=1 ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 0 ;;
    *) echo "unknown option: $arg (expected --from-env and/or --dry-run)" >&2; exit 2 ;;
  esac
done

if [ "$FROM_ENV" = 1 ]; then
  tok="${RAINDROP_TOKEN:?RAINDROP_TOKEN is not set}"
else
  printf 'Raindrop API token (input hidden): ' >&2
  read -rs tok; printf '\n' >&2
fi
tok="${tok%$'\r'}"
[ -n "$tok" ] || { echo "empty token, aborting" >&2; exit 1; }
case "$tok" in
  *[[:space:]]*) echo "token contains whitespace, aborting (a paste picked up more than the token?)" >&2; exit 1 ;;
esac

if [ "$DRY_RUN" = 1 ]; then
  echo "dry run: would store a ${#tok}-character token in the login keychain as service '$SERVICE', account '$ACCOUNT', replacing any existing item under that service. Nothing was written." >&2
  unset tok
  exit 0
fi

security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT" -w "$tok"
unset tok
echo "Stored in the login keychain as service '$SERVICE' (any previous item under that service was replaced)." >&2
echo "Verify with: ${RAINDROP_SYNC_ROOT:-$HOME/.raindrop-sync}/bin/get-token.sh | wc -c   # prints a length, not the token" >&2
