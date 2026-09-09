#!/usr/bin/env bash
# Bootstrap raindrop-brave-sync on macOS.
#
# Creates the runtime tree (default ~/.raindrop-sync), generates a pinned extension
# signing key, derives the extension id, resolves the Brave profile, installs the
# native-messaging host manifests and the launchd fallback agent, and writes
# config.json so every other component agrees on the same paths.
#
# Idempotent: re-running keeps key.pem (so the extension id is stable), rewrites the
# manifests, re-renders the plist and re-bootstraps the agent.
#
# Overrides (all optional; tests and CI point every one of them into a sandbox):
#   RAINDROP_SYNC_ROOT            runtime tree                 default ~/.raindrop-sync
#   RAINDROP_SYNC_PROFILE         Brave profile directory (the one holding Bookmarks);
#                                 otherwise discovered from Brave's Local State
#   RAINDROP_SYNC_PYTHON          interpreter                  default /usr/bin/python3
#   RAINDROP_SYNC_NMH_CHROME      Chrome's NativeMessagingHosts dir (the one Brave reads)
#   RAINDROP_SYNC_NMH_BRAVE       Brave's own NativeMessagingHosts dir
#   RAINDROP_SYNC_LAUNCHAGENTS    where the plist goes         default ~/Library/LaunchAgents
#   RAINDROP_SYNC_SKILLS          Claude Code skills dir       default ~/.claude/skills
#   RAINDROP_SYNC_SKIP_PROFILE_CHECK=1   do not require a Brave profile (CI, tests)
#   SKIP_LAUNCHCTL=1              write the plist but never call launchctl
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${RAINDROP_SYNC_ROOT:-$HOME/.raindrop-sync}"
HOSTID="com.raindrop_sync.host"
LABEL="com.raindrop-sync.apply"
PY="${RAINDROP_SYNC_PYTHON:-/usr/bin/python3}"

# Brave reads native-messaging manifests from CHROME's directory, not its own.
# This is not a typo - see RESEARCH.md. A manifest in Brave's own directory is
# silently ignored and the extension reports "host not found".
NMH_CHROME="${RAINDROP_SYNC_NMH_CHROME:-$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts}"
NMH_BRAVE="${RAINDROP_SYNC_NMH_BRAVE:-$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts}"
LAUNCH_AGENTS="${RAINDROP_SYNC_LAUNCHAGENTS:-$HOME/Library/LaunchAgents}"
# SKIP_LAUNCHCTL=1 writes the plist but never calls launchctl: for CI, the test suite,
# and a dry run on a machine that should not get an agent.
SKIP_LAUNCHCTL="${SKIP_LAUNCHCTL:-0}"
SKIP_PROFILE_CHECK="${RAINDROP_SYNC_SKIP_PROFILE_CHECK:-0}"

say()  { printf '\033[1m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!!\033[0m %s\n' "$1" >&2; }

# ── 0. interpreter ──────────────────────────────────────────────────────────
# On a stock Mac /usr/bin/python3 is a stub that only offers to install the
# Command Line Tools. Find that out before creating anything.
if ! "$PY" -c 'import sys; assert sys.version_info >= (3, 9)' >/dev/null 2>&1; then
  echo "error: $PY is missing or older than Python 3.9." >&2
  echo "Install the Xcode Command Line Tools: xcode-select --install" >&2
  echo "(or point RAINDROP_SYNC_PYTHON at a Python 3.9 or newer interpreter)" >&2
  exit 1
fi

# ── 1. Brave profile ────────────────────────────────────────────────────────
# Files only, no Brave API: RAINDROP_SYNC_PROFILE wins; otherwise each channel's
# "Local State" names the last-used profile and the first one with a Bookmarks
# file is taken. Done before anything is created so a miss leaves no half-install.
say "Resolving the Brave profile"
if ! PROFILE_INFO="$("$PY" - "$HOME" "${RAINDROP_SYNC_PROFILE:-}" "$SKIP_PROFILE_CHECK" <<'PYEOF'
import json, os, sys
home, explicit, skip = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
CHANNELS = ("Brave-Browser", "Brave-Browser-Beta", "Brave-Browser-Nightly")
checked, profile = [], None
if explicit:
    checked.append(explicit)
    if os.path.isfile(os.path.join(explicit, "Bookmarks")):
        profile = explicit
else:
    base = os.path.join(home, "Library", "Application Support", "BraveSoftware")
    for channel in CHANNELS:
        cdir = os.path.join(base, channel)
        last = "Default"
        try:
            with open(os.path.join(cdir, "Local State"), encoding="utf-8") as f:
                last = (json.load(f).get("profile") or {}).get("last_used") or "Default"
        except (OSError, ValueError, AttributeError):
            pass
        cand = os.path.join(cdir, last)
        checked.append(cand)
        if os.path.isfile(os.path.join(cand, "Bookmarks")):
            profile = cand
            break
sync = False
if profile is None:
    sys.stderr.write("No Brave Bookmarks file found. Checked:\n")
    for c in checked:
        sys.stderr.write("  %s/Bookmarks\n" % c)
    if explicit and skip:
        sys.stderr.write("RAINDROP_SYNC_SKIP_PROFILE_CHECK=1: keeping RAINDROP_SYNC_PROFILE=%s unverified\n" % explicit)
        profile = explicit
    elif skip:
        sys.stderr.write("RAINDROP_SYNC_SKIP_PROFILE_CHECK=1: continuing without a profile\n")
    else:
        sys.stderr.write("To fix: launch Brave once (it writes Bookmarks after the first bookmark is "
                         "added), or set RAINDROP_SYNC_PROFILE=<profile dir>, then re-run.\n")
        sys.exit(3)
else:
    try:
        with open(os.path.join(profile, "Bookmarks"), encoding="utf-8") as f:
            doc = json.load(f)
        sync = isinstance(doc, dict) and "sync_metadata" in doc
    except (OSError, ValueError) as e:
        sys.stderr.write("warning: %s/Bookmarks is not readable JSON (%s); Brave Sync check skipped\n"
                         % (profile, e))
print(profile or "")
print("1" if sync else "0")
PYEOF
)"; then
  echo "error: no Brave profile resolved; nothing was installed." >&2
  exit 1
fi
PROFILE="$(printf '%s\n' "$PROFILE_INFO" | sed -n '1p')"
BRAVE_SYNC="$(printf '%s\n' "$PROFILE_INFO" | sed -n '2p')"
if [ -n "$PROFILE" ]; then
  say "Brave profile: $PROFILE"
else
  say "Brave profile: none (RAINDROP_SYNC_SKIP_PROFILE_CHECK=1)"
fi
if [ "$BRAVE_SYNC" = 1 ]; then
  cat >&2 <<EOF

!!! WARNING: Brave Sync is ON for this profile ($PROFILE/Bookmarks carries sync_metadata).
!!! The launchd file-writer agent will NOT be installed: external nodes written into a
!!! synced profile corrupt the sync metadata and upload the damage to every device.
!!! The extension path still works (it goes through the bookmarks API), so load it as
!!! shown at the end. To use the file writer too, turn off bookmark sync in Brave's
!!! sync settings and re-run this script.

EOF
fi

# ── 2. runtime tree ─────────────────────────────────────────────────────────
say "Creating $ROOT"
mkdir -p "$ROOT/bin" "$ROOT/backups" "$ROOT/log" "$ROOT/extension"
chmod 700 "$ROOT"

say "Installing scripts"
install -m 755 "$REPO/src/apply_brave.py"   "$ROOT/bin/apply_brave.py"
install -m 755 "$REPO/src/native_host.py"   "$ROOT/bin/native_host.py"
install -m 755 "$REPO/scripts/get-token.sh"  "$ROOT/bin/get-token.sh"
install -m 755 "$REPO/scripts/seed-token.sh" "$ROOT/bin/seed-token.sh"
install -m 755 "$REPO/scripts/doctor.sh"     "$ROOT/bin/doctor.sh"

# Brave starts the native host with a near-empty environment and runs the manifest
# "path" directly, so the runtime root and the interpreter are pinned in a wrapper
# rather than inherited or resolved through PATH.
WRAPPER="$ROOT/bin/native_host"
cat > "$WRAPPER" <<SH
#!/bin/sh
# Generated by bootstrap.sh; re-run it rather than editing this file.
export RAINDROP_SYNC_ROOT="$ROOT"
exec "$PY" "$ROOT/bin/native_host.py"
SH
chmod 755 "$WRAPPER"

# bootstrap writes .env itself: a clone need not carry .env.example for this to work.
if [ ! -f "$ROOT/.env" ]; then
  (umask 077; printf 'RAINDROP_TOKEN=\n' > "$ROOT/.env")
  say "Created $ROOT/.env (empty: seed the keychain or fill it in, see the end of this output)"
fi
chmod 600 "$ROOT/.env"

# ── 3. extension identity ───────────────────────────────────────────────────
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
cp "$REPO/extension/sw.js" "$ROOT/extension/sw.js"
EXT_ID="$("$PY" - "$ROOT" "$REPO" <<'PYEOF'
import base64, hashlib, json, subprocess, sys
root, repo = sys.argv[1], sys.argv[2]
der = subprocess.run(["openssl", "rsa", "-in", root + "/key.pem", "-pubout", "-outform", "DER"],
                     capture_output=True, check=True).stdout
# Chromium extension id: first 16 bytes of sha256(DER SubjectPublicKeyInfo),
# hex digits mapped 0-f -> a-p.
ext_id = "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])
with open(repo + "/extension/manifest.json", encoding="utf-8") as f:
    m = json.load(f)
m["key"] = base64.b64encode(der).decode()
with open(root + "/extension/manifest.json", "w", encoding="utf-8") as f:
    json.dump(m, f, indent=2); f.write("\n")
with open(root + "/.extension-identity.json", "w", encoding="utf-8") as f:
    json.dump({"id": ext_id}, f, indent=1); f.write("\n")
print(ext_id)
PYEOF
)"
say "Extension id: $EXT_ID"

# ── 4. host manifests, launchd plist, config.json ───────────────────────────
say "Installing native-messaging host manifests"
PLIST="$LAUNCH_AGENTS/$LABEL.plist"
if [ "$BRAVE_SYNC" = 1 ]; then
  PLIST_OUT=""
else
  mkdir -p "$LAUNCH_AGENTS"
  PLIST_OUT="$PLIST"
fi
"$PY" - "$ROOT" "$PY" "$PROFILE" "$BRAVE_SYNC" "$EXT_ID" "$HOSTID" "$WRAPPER" \
       "$NMH_CHROME" "$NMH_BRAVE" "$REPO/launchd/$LABEL.plist.template" "$PLIST_OUT" <<'PYEOF'
import json, os, plistlib, sys
from datetime import datetime, timezone
(root, py, profile, sync, ext_id, hostid, wrapper,
 nmh_chrome, nmh_brave, template, plist_out) = sys.argv[1:12]

manifest = {
    "name": hostid,
    "description": "raindrop-brave-sync native messaging host",
    "path": wrapper,
    "type": "stdio",
    "allowed_origins": ["chrome-extension://%s/" % ext_id],
}
for d in (nmh_chrome, nmh_brave):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, hostid + ".json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2); f.write("\n")

# The plist comes from the template so the manual path in SETUP.md and this one
# cannot drift; plistlib keeps the output well-formed whatever the paths contain.
if plist_out:
    with open(template, "rb") as f:
        plist = plistlib.load(f)
    def fill(v):
        if isinstance(v, str):
            return v.replace("__PYTHON__", py).replace("__ROOT__", root).replace("__PROFILE__", profile)
        if isinstance(v, list):
            return [fill(x) for x in v]
        if isinstance(v, dict):
            return {k: fill(x) for k, x in v.items()}
        return v
    plist = fill(plist)
    env = plist.setdefault("EnvironmentVariables", {})
    env["RAINDROP_SYNC_ROOT"] = root
    if profile:
        env["RAINDROP_SYNC_PROFILE"] = profile
    else:
        env.pop("RAINDROP_SYNC_PROFILE", None)
    with open(plist_out, "wb") as f:
        plistlib.dump(plist, f)
    with open(plist_out, "rb") as f:
        plistlib.load(f)          # prove it parses before launchd sees it

config = {
    "root": root,
    "python": py,
    "profile": profile or None,
    "extension_id": ext_id,
    "brave_sync": sync == "1",
    "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
}
with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2); f.write("\n")
PYEOF
say "  installed into Chrome's dir (the one Brave actually reads) and Brave's"
say "  wrote $ROOT/config.json"

# ── 5. ledger ───────────────────────────────────────────────────────────────
say "Initialising state.db"
if "$PY" "$ROOT/bin/native_host.py" --init-db </dev/null >/dev/null 2>&1 && [ -f "$ROOT/state.db" ]; then
  say "  $ROOT/state.db ready"
else
  warn "state.db was not initialised (native_host.py --init-db failed); the host creates it on first contact"
fi

# ── 6. launchd fallback ─────────────────────────────────────────────────────
say "Installing launchd fallback agent"
UID_NOW="$(id -u)"
if [ "$BRAVE_SYNC" = 1 ]; then
  if [ "$SKIP_LAUNCHCTL" != 1 ]; then
    launchctl bootout "gui/$UID_NOW/$LABEL" 2>/dev/null || true
  fi
  rm -f "$PLIST"
  AGENT_STATE="not installed (Brave Sync is on for this profile)"
  say "  $AGENT_STATE"
elif [ "$SKIP_LAUNCHCTL" = 1 ]; then
  AGENT_STATE="written to $PLIST but not loaded (SKIP_LAUNCHCTL=1)"
  say "  $AGENT_STATE"
else
  launchctl bootout "gui/$UID_NOW/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NOW" "$PLIST"
  AGENT_STATE="loaded as $LABEL (runs now, then every 15 minutes)"
  say "  $AGENT_STATE"
fi

# ── 7. Claude Code skill ────────────────────────────────────────────────────
say "Installing the classification skill"
if [ -n "${RAINDROP_SYNC_SKILLS:-}" ]; then
  SKILLS_DIR="$RAINDROP_SYNC_SKILLS"
elif [ -d "$HOME/.claude" ]; then
  SKILLS_DIR="$HOME/.claude/skills"
else
  SKILLS_DIR=""
fi
if [ -n "$SKILLS_DIR" ]; then
  mkdir -p "$SKILLS_DIR/raindrop-sync"
  cp "$REPO/skill/raindrop-sync/SKILL.md" "$SKILLS_DIR/raindrop-sync/SKILL.md"
  say "  -> $SKILLS_DIR/raindrop-sync/SKILL.md  (invoke with /raindrop-sync)"
else
  say "  skipped: $HOME/.claude not found (Claude Code is not installed). Install it and re-run,"
  say "  or set RAINDROP_SYNC_SKILLS=<skills dir> to install the skill somewhere else."
fi

cat <<DONE

──────────────────────────────────────────────────────────────────────────
 Bootstrap complete.

 Runtime root:   $ROOT
 Brave profile:  ${PROFILE:-none resolved}
 Extension id:   $EXT_ID
 launchd agent:  $AGENT_STATE

 Next, in order (see SETUP.md):
   1. Give it your Raindrop token, ONE of:
        $ROOT/bin/seed-token.sh     # login keychain (preferred for unattended runs)
        edit $ROOT/.env             # RAINDROP_TOKEN=...  a non-empty .env wins over the keychain
   2. Load the extension UNPACKED:
        brave://extensions -> Developer mode -> Load unpacked -> $ROOT/extension
      Confirm the id reads: $EXT_ID
      DO NOT pack a .crx: it permanently changes the extension id (RESEARCH.md).
   3. Check the install:
        $ROOT/bin/doctor.sh
   4. Run the first classification pass to build YOUR taxonomy (SETUP.md step 7).

 Re-run this script any time: it keeps key.pem and refreshes everything else.
 Agent status by hand: launchctl print gui/$UID_NOW/$LABEL
──────────────────────────────────────────────────────────────────────────
DONE
