#!/usr/bin/env bash
# Health check for a raindrop-brave-sync install. One line per check, prefixed "ok", "warn"
# or "FAIL"; exit status 0 iff nothing failed. Never prints the API token.
#
# Runs from the checkout (scripts/doctor.sh) or from the runtime tree (bin/doctor.sh) and
# honours the same overrides as bootstrap.sh: RAINDROP_SYNC_ROOT, RAINDROP_SYNC_PROFILE,
# RAINDROP_SYNC_PYTHON, RAINDROP_SYNC_NMH_CHROME, RAINDROP_SYNC_NMH_BRAVE,
# RAINDROP_SYNC_LAUNCHAGENTS, SKIP_LAUNCHCTL, RAINDROP_SYNC_SKIP_PROFILE_CHECK,
# RAINDROP_SYNC_NO_KEYCHAIN, RAINDROP_SYNC_KEYCHAIN_SERVICE.
#
# Bash 3.2 (the one macOS ships): no associative arrays, no mapfile.
set -uo pipefail

ROOT="${RAINDROP_SYNC_ROOT:-$HOME/.raindrop-sync}"
PY="${RAINDROP_SYNC_PYTHON:-/usr/bin/python3}"
HOSTID="com.raindrop_sync.host"
LABEL="com.raindrop-sync.apply"
NMH_CHROME="${RAINDROP_SYNC_NMH_CHROME:-$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts}"
NMH_BRAVE="${RAINDROP_SYNC_NMH_BRAVE:-$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts}"
LAUNCH_AGENTS="${RAINDROP_SYNC_LAUNCHAGENTS:-$HOME/Library/LaunchAgents}"
SKIP_LAUNCHCTL="${SKIP_LAUNCHCTL:-0}"
SKIP_PROFILE_CHECK="${RAINDROP_SYNC_SKIP_PROFILE_CHECK:-0}"
CFG="$ROOT/config.json"

fails=0; warns=0; oks=0
ok()   { printf 'ok    %s\n' "$1"; oks=$((oks + 1)); }
warn() { printf 'warn  %s\n' "$1"; warns=$((warns + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
finish() {
  printf 'summary: %d ok, %d warn, %d FAIL\n' "$oks" "$warns" "$fails"
  [ "$fails" -eq 0 ]
  exit $?
}

# ── interpreter ─────────────────────────────────────────────────────────────
if PYV="$("$PY" -c 'import sys; assert sys.version_info >= (3, 9); print(sys.version.split()[0])' 2>/dev/null)"; then
  ok "python $PYV at $PY"
else
  fail "$PY is missing or older than 3.9: run xcode-select --install, or set RAINDROP_SYNC_PYTHON"
  finish
fi

# ── runtime tree ────────────────────────────────────────────────────────────
if [ ! -d "$ROOT" ]; then
  fail "runtime root $ROOT does not exist: run scripts/bootstrap.sh"
  finish
fi
mode="$(stat -f '%Lp' "$ROOT" 2>/dev/null || stat -c '%a' "$ROOT" 2>/dev/null)"
if [ "$mode" = "700" ]; then ok "root $ROOT (mode 700)"; else warn "root $ROOT is mode $mode, expected 700 (.env and key.pem live here)"; fi

for f in bin/apply_brave.py bin/native_host.py bin/get-token.sh bin/seed-token.sh; do
  if [ -x "$ROOT/$f" ]; then ok "$f installed"; else fail "$f missing or not executable: re-run scripts/bootstrap.sh"; fi
done
WRAPPER="$ROOT/bin/native_host"
if [ -x "$WRAPPER" ]; then
  if grep -q "RAINDROP_SYNC_ROOT=\"$ROOT\"" "$WRAPPER" 2>/dev/null; then
    ok "host wrapper bin/native_host pins RAINDROP_SYNC_ROOT=$ROOT"
  else
    fail "host wrapper bin/native_host does not export RAINDROP_SYNC_ROOT=$ROOT: re-run scripts/bootstrap.sh"
  fi
else
  fail "host wrapper bin/native_host missing: re-run scripts/bootstrap.sh"
fi

if [ -f "$ROOT/key.pem" ]; then
  kmode="$(stat -f '%Lp' "$ROOT/key.pem" 2>/dev/null || stat -c '%a' "$ROOT/key.pem" 2>/dev/null)"
  if [ "$kmode" = "600" ]; then ok "key.pem present (mode 600)"; else warn "key.pem is mode $kmode, expected 600"; fi
else
  fail "key.pem missing: re-run scripts/bootstrap.sh (this mints a NEW extension id)"
fi

# ── config.json ─────────────────────────────────────────────────────────────
EXT_ID=""; PROFILE_CFG=""; BRAVE_SYNC="false"
if [ -f "$CFG" ]; then
  if CFG_LINES="$("$PY" - "$CFG" <<'PYEOF' 2>/dev/null
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    c = json.load(f)
if not isinstance(c, dict):
    raise SystemExit(1)
print(c.get("extension_id") or "")
print(c.get("profile") or "")
print("true" if c.get("brave_sync") else "false")
print(c.get("python") or "")
PYEOF
)"; then
    EXT_ID="$(printf '%s\n' "$CFG_LINES" | sed -n '1p')"
    PROFILE_CFG="$(printf '%s\n' "$CFG_LINES" | sed -n '2p')"
    BRAVE_SYNC="$(printf '%s\n' "$CFG_LINES" | sed -n '3p')"
    CFG_PY="$(printf '%s\n' "$CFG_LINES" | sed -n '4p')"
    ok "config.json parses (extension_id ${EXT_ID:-unset}, brave_sync $BRAVE_SYNC)"
    if [ -n "$CFG_PY" ] && [ "$CFG_PY" != "$PY" ]; then
      warn "config.json records python $CFG_PY but this check ran $PY"
    fi
  else
    fail "config.json is not valid JSON: re-run scripts/bootstrap.sh"
  fi
else
  fail "config.json missing: re-run scripts/bootstrap.sh"
fi

# ── extension identity ──────────────────────────────────────────────────────
if [ -f "$ROOT/extension/manifest.json" ]; then
  if DERIVED="$("$PY" - "$ROOT/extension/manifest.json" <<'PYEOF' 2>/dev/null
import base64, hashlib, json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    m = json.load(f)
key = m.get("key")
if not key:
    raise SystemExit(2)
der = base64.b64decode(key)
print("".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32]))
PYEOF
)"; then
    if [ -n "$EXT_ID" ] && [ "$DERIVED" = "$EXT_ID" ]; then
      ok "extension/manifest.json key derives id $DERIVED, matching config.json"
    elif [ -z "$EXT_ID" ]; then
      warn "extension/manifest.json key derives id $DERIVED; config.json has no extension_id to compare"
      EXT_ID="$DERIVED"
    else
      fail "extension/manifest.json key derives id $DERIVED but config.json says $EXT_ID: re-run scripts/bootstrap.sh"
    fi
  else
    fail "extension/manifest.json has no \"key\" (the id would depend on the folder path): re-run scripts/bootstrap.sh"
  fi
else
  fail "extension/manifest.json missing: re-run scripts/bootstrap.sh"
fi
[ -f "$ROOT/extension/sw.js" ] && ok "extension/sw.js present" || fail "extension/sw.js missing: re-run scripts/bootstrap.sh"

# ── native-messaging host manifests ─────────────────────────────────────────
for d in "$NMH_CHROME" "$NMH_BRAVE"; do
  mf="$d/$HOSTID.json"
  if [ ! -f "$mf" ]; then
    fail "host manifest missing: $mf (re-run scripts/bootstrap.sh)"
    continue
  fi
  if MSG="$("$PY" - "$mf" "$HOSTID" "$EXT_ID" "$WRAPPER" <<'PYEOF' 2>&1
import json, os, sys
path, hostid, ext_id, wrapper = sys.argv[1:5]
with open(path, encoding="utf-8") as f:
    m = json.load(f)
problems = []
if m.get("name") != hostid:
    problems.append("name is %r, expected %r" % (m.get("name"), hostid))
p = m.get("path") or ""
if not os.path.isabs(p):
    problems.append("path is not absolute")
elif not os.path.isfile(p):
    problems.append("path does not exist: %s" % p)
elif not os.access(p, os.X_OK):
    problems.append("path is not executable: %s" % p)
elif p != wrapper:
    problems.append("path is %s, expected the wrapper %s" % (p, wrapper))
origin = "chrome-extension://%s/" % ext_id
if ext_id and origin not in (m.get("allowed_origins") or []):
    problems.append("allowed_origins lacks %s" % origin)
if m.get("type") != "stdio":
    problems.append("type is %r, expected 'stdio'" % m.get("type"))
if problems:
    print("; ".join(problems)); raise SystemExit(1)
PYEOF
)"; then
    ok "host manifest $mf"
  else
    fail "host manifest $mf: ${MSG:-not valid JSON}"
  fi
done

# ── the host answers, through the wrapper, with the environment Brave gives it ──
if [ -x "$WRAPPER" ]; then
  if REPLY="$("$PY" - "$WRAPPER" "$HOME" <<'PYEOF' 2>/dev/null
import json, struct, subprocess, sys
wrapper, home = sys.argv[1], sys.argv[2]
msg = b'{"op":"ping"}'
p = subprocess.run([wrapper], input=struct.pack("@I", len(msg)) + msg, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": home}, timeout=20)
out = p.stdout
if len(out) < 4:
    raise SystemExit(1)
(n,) = struct.unpack("@I", out[:4])
reply = json.loads(out[4:4 + n].decode("utf-8"))
if reply.get("ok") is not True:
    raise SystemExit(1)
print(reply.get("version", "?"))
PYEOF
)"; then
    ok "native host answers ping through the wrapper (version $REPLY)"
  else
    fail "native host did not answer ping through $WRAPPER: check log/native_host.log"
  fi
fi

# ── Brave profile and Brave Sync ────────────────────────────────────────────
PROFILE="${RAINDROP_SYNC_PROFILE:-$PROFILE_CFG}"
if [ -z "$PROFILE" ]; then
  warn "no Brave profile recorded (RAINDROP_SYNC_SKIP_PROFILE_CHECK=1 at install?): the file writer has nothing to write to"
elif [ ! -f "$PROFILE/Bookmarks" ]; then
  if [ "$SKIP_PROFILE_CHECK" = 1 ]; then
    warn "profile $PROFILE has no Bookmarks file (RAINDROP_SYNC_SKIP_PROFILE_CHECK=1)"
  else
    fail "profile $PROFILE has no Bookmarks file: launch Brave once, or re-run bootstrap with RAINDROP_SYNC_PROFILE"
  fi
else
  ok "profile $PROFILE has a Bookmarks file"
  SYNC_NOW="$("$PY" -c 'import json,sys
try:
    with open(sys.argv[1], encoding="utf-8") as f: d = json.load(f)
    print("true" if isinstance(d, dict) and "sync_metadata" in d else "false")
except Exception: print("unreadable")' "$PROFILE/Bookmarks" 2>/dev/null)"
  case "$SYNC_NOW" in
    true)  warn "Brave Sync is ON for this profile: the file writer refuses to run; the extension still applies" ;;
    false) ok "Brave Sync is off for this profile" ;;
    *)     warn "$PROFILE/Bookmarks is not readable JSON right now (Brave may be mid-write)" ;;
  esac
  if [ "$SYNC_NOW" = true ] && [ "$BRAVE_SYNC" = false ]; then
    warn "config.json says brave_sync false but the file carries sync_metadata: re-run scripts/bootstrap.sh"
  elif [ "$SYNC_NOW" = false ] && [ "$BRAVE_SYNC" = true ]; then
    warn "config.json says brave_sync true but the file has no sync_metadata: re-run scripts/bootstrap.sh to install the agent"
  fi
fi

# ── launchd agent ───────────────────────────────────────────────────────────
PLIST="$LAUNCH_AGENTS/$LABEL.plist"
if [ "$SKIP_LAUNCHCTL" = 1 ]; then
  if [ "$BRAVE_SYNC" = true ]; then
    ok "launchd check skipped (SKIP_LAUNCHCTL=1); no plist expected while Brave Sync is on"
  elif [ -f "$PLIST" ]; then
    ok "launchd check skipped (SKIP_LAUNCHCTL=1); plist present at $PLIST"
  else
    fail "plist missing at $PLIST: re-run scripts/bootstrap.sh"
  fi
elif [ "$BRAVE_SYNC" = true ]; then
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    warn "launchd agent $LABEL is loaded although Brave Sync is on: re-run scripts/bootstrap.sh to remove it"
  else
    ok "launchd agent absent, as expected while Brave Sync is on"
  fi
else
  if [ ! -f "$PLIST" ]; then
    fail "plist missing at $PLIST: re-run scripts/bootstrap.sh"
  elif launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    ok "launchd agent $LABEL is loaded"
  else
    fail "launchd agent $LABEL is not loaded: launchctl bootstrap gui/$(id -u) \"$PLIST\" (or re-run scripts/bootstrap.sh)"
  fi
fi

# ── token (never printed) ───────────────────────────────────────────────────
if [ -x "$ROOT/bin/get-token.sh" ]; then
  nbytes="$("$ROOT/bin/get-token.sh" 2>/dev/null | wc -c | tr -d ' ')"
  rc=${PIPESTATUS[0]}
  if [ "$rc" -eq 0 ] && [ "${nbytes:-0}" -gt 0 ]; then
    ok "token: found ($nbytes bytes)"
  else
    warn "token: missing. Run $ROOT/bin/seed-token.sh (keychain) or fill RAINDROP_TOKEN= in $ROOT/.env"
  fi
fi

# ── staged work and the ledger ──────────────────────────────────────────────
if [ -f "$ROOT/desired.json" ]; then
  if N="$("$PY" -c 'import json,sys
with open(sys.argv[1], encoding="utf-8") as f: d = json.load(f)
if not isinstance(d, list): raise SystemExit(1)
print(len(d))' "$ROOT/desired.json" 2>/dev/null)"; then
    ok "desired.json parses ($N staged entries)"
  else
    fail "desired.json is not a JSON list: Phase A wrote something malformed"
  fi
else
  warn "desired.json absent: nothing staged yet (run /raindrop-sync in Claude Code)"
fi

if [ -f "$ROOT/state.db" ]; then
  if MISSING="$("$PY" -c 'import sqlite3,sys
con = sqlite3.connect(sys.argv[1])
have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type=\"table\"")}
print(" ".join(sorted({"bookmarks","meta","runs","extension_applied"} - have)))' "$ROOT/state.db" 2>/dev/null)"; then
    if [ -z "$MISSING" ]; then
      ok "state.db has bookmarks, meta, runs, extension_applied"
    else
      fail "state.db lacks table(s): $MISSING (run $ROOT/bin/native_host.py --init-db)"
    fi
  else
    fail "state.db is not readable as SQLite"
  fi
else
  fail "state.db missing: run $ROOT/bin/native_host.py --init-db (bootstrap does this)"
fi

# ── has the extension ever talked to the host? ──────────────────────────────
contacted=0
if [ -f "$ROOT/state.db" ]; then
  rows="$("$PY" -c 'import sqlite3,sys
con = sqlite3.connect(sys.argv[1])
try: print(con.execute("SELECT COUNT(*) FROM extension_applied").fetchone()[0])
except Exception: print(0)' "$ROOT/state.db" 2>/dev/null)"
  [ "${rows:-0}" -gt 0 ] && contacted=1
fi
if [ "$contacted" = 0 ] && [ -f "$ROOT/log/native_host.log" ] && grep -q 'pending\[' "$ROOT/log/native_host.log" 2>/dev/null; then
  contacted=1
fi
if [ "$contacted" = 1 ]; then
  ok "the extension has contacted the host (ledger rows or pending[] lines present)"
else
  warn "the extension has not contacted the host yet: load $ROOT/extension unpacked in brave://extensions (SETUP.md step 3)"
fi

# ── logs, informational ─────────────────────────────────────────────────────
if [ -s "$ROOT/log/apply.err.log" ]; then
  warn "log/apply.err.log is not empty ($(wc -l < "$ROOT/log/apply.err.log" | tr -d ' ') lines): read it"
fi
if [ -f "$ROOT/log/apply.out.log" ]; then
  last="$(tail -1 "$ROOT/log/apply.out.log" 2>/dev/null)"
  case "$last" in
    *ABORT*|*FAILED*) warn "last file-writer line: $last" ;;
    "") ok "log/apply.out.log is empty (the agent has not run yet)" ;;
    *) ok "last file-writer line: $last" ;;
  esac
fi

finish
