#!/usr/bin/env bash
# Bootstrap raindrop-brave-sync on macOS.
#
# Creates ~/.raindrop-sync, generates a pinned extension signing key, derives the
# extension id, installs the native-messaging host manifest and the launchd fallback.
# Idempotent: safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${RAINDROP_SYNC_ROOT:-$HOME/.raindrop-sync}"
HOSTID="com.raindrop_sync.host"
PY="${RAINDROP_SYNC_PYTHON:-/usr/bin/python3}"

# Brave reads native-messaging manifests from CHROME's directory, not its own.
# This is not a typo — see RESEARCH.md. A manifest in Brave's own directory is
# silently ignored and the extension reports "host not found".
NMH_CHROME="${RAINDROP_SYNC_NMH_CHROME:-$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts}"
NMH_BRAVE="${RAINDROP_SYNC_NMH_BRAVE:-$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts}"
LAUNCH_AGENTS="${RAINDROP_SYNC_LAUNCHAGENTS:-$HOME/Library/LaunchAgents}"
# Set SKIP_LAUNCHCTL=1 to install the plist without loading it (used by the test suite).
SKIP_LAUNCHCTL="${SKIP_LAUNCHCTL:-0}"

say() { printf '\033[1m==>\033[0m %s\n' "$1"; }

say "Creating $ROOT"
mkdir -p "$ROOT"/{bin,backups,log}
chmod 700 "$ROOT"

say "Installing scripts"
install -m 755 "$REPO/src/apply_brave.py"  "$ROOT/bin/apply_brave.py"
install -m 755 "$REPO/src/native_host.py"  "$ROOT/bin/native_host.py"

say "Installing token helpers"
install -m 755 "$REPO/scripts/get-token.sh"  "$ROOT/bin/get-token.sh"
install -m 755 "$REPO/scripts/seed-token.sh" "$ROOT/bin/seed-token.sh"

say "Installing the classification skill"
SKILL_DEST="${RAINDROP_SYNC_SKILLS:-$HOME/.claude/skills}/raindrop-sync"
mkdir -p "$SKILL_DEST"
cp "$REPO/skill/raindrop-sync/SKILL.md" "$SKILL_DEST/SKILL.md"
say "  -> $SKILL_DEST/SKILL.md  (invoke with /raindrop-sync)"

if [ ! -f "$ROOT/.env" ]; then
  cp "$REPO/.env.example" "$ROOT/.env"; chmod 600 "$ROOT/.env"
  say "Created $ROOT/.env — add your Raindrop token (see SETUP.md)"
fi

# ── extension identity ──────────────────────────────────────────────────────
# The key is PINNED into manifest.json so the extension id is stable. Without it
# the id derives from the install path, and moving the folder silently breaks the
# native-host allowlist.
if [ ! -f "$ROOT/key.pem" ]; then
  say "Generating extension signing key"
  openssl genrsa -out "$ROOT/key.pem" 2048 2>/dev/null
  chmod 600 "$ROOT/key.pem"
else
  say "Reusing existing key.pem (delete it to mint a new extension id)"
fi

say "Deriving extension id"
EXT_DIR="$ROOT/extension"; mkdir -p "$EXT_DIR"
cp "$REPO/extension/sw.js" "$EXT_DIR/sw.js"
"$PY" - "$ROOT" "$REPO" <<'PYEOF'
import base64, hashlib, json, subprocess, sys, os
root, repo = sys.argv[1], sys.argv[2]
der = subprocess.run(["openssl","rsa","-in",f"{root}/key.pem","-pubout","-outform","DER"],
                     capture_output=True, check=True).stdout
# Chromium extension id: first 16 bytes of sha256(DER SubjectPublicKeyInfo),
# hex digits mapped 0-f -> a-p.
ext_id = ''.join(chr(ord('a') + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])
m = json.load(open(f"{repo}/extension/manifest.json"))
m["key"] = base64.b64encode(der).decode()
json.dump(m, open(f"{root}/extension/manifest.json", "w"), indent=2)
json.dump({"id": ext_id}, open(f"{root}/.extension-identity.json", "w"), indent=1)
print(ext_id)
PYEOF
EXT_ID="$("$PY" -c "import json,sys;print(json.load(open('$ROOT/.extension-identity.json'))['id'])")"
say "Extension id: $EXT_ID"

# ── native messaging host ───────────────────────────────────────────────────
say "Installing native-messaging host manifest"
for d in "$NMH_CHROME" "$NMH_BRAVE"; do
  mkdir -p "$d"
  cat > "$d/$HOSTID.json" <<JSON
{
  "name": "$HOSTID",
  "description": "raindrop-brave-sync native messaging host",
  "path": "$ROOT/bin/native_host.py",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
JSON
done
say "  installed into Chrome's dir (the one Brave actually reads) and Brave's"

# ── launchd fallback ────────────────────────────────────────────────────────
say "Installing launchd fallback agent"
mkdir -p "$LAUNCH_AGENTS"
PLIST="$LAUNCH_AGENTS/com.raindrop-sync.apply.plist"
sed -e "s#__PYTHON__#$PY#g" -e "s#__ROOT__#$ROOT#g" \
    "$REPO/launchd/com.raindrop-sync.apply.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
if [ "$SKIP_LAUNCHCTL" = "1" ]; then
  say "  SKIP_LAUNCHCTL=1 — plist written but not loaded"
else
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
fi

cat <<DONE

──────────────────────────────────────────────────────────────────────────
 Bootstrap complete.

 Next, in order (see SETUP.md):
   1. Put your Raindrop token in  $ROOT/.env
   2. Load the extension UNPACKED:
        brave://extensions -> Developer mode -> Load unpacked
        -> $ROOT/extension
      Confirm the id reads: $EXT_ID
      DO NOT pack a .crx — it permanently poisons the extension id. RESEARCH.md
   3. Run the first classification pass to build YOUR taxonomy.

 Verify:
   tail $ROOT/log/native_host.log     # expect "host start" then "pending: N of M"
   launchctl list | grep raindrop
──────────────────────────────────────────────────────────────────────────
DONE
