#!/usr/bin/env bash
# Fresh-install rehearsal: what a first-time user's Mac goes through, with every path redirected
# into a sandbox so nothing real is touched. Run by .github/workflows/install.yml on a macOS
# runner and by hand before a release:
#
#   scripts/ci-rehearsal.sh [sandbox dir]
#
# It installs from a COPY of the checkout with .env.example removed (the worst case for a
# clone), discovers a Brave Beta "Profile 1" from a fake Local State, runs the doctor, talks
# to the host through the wrapper with the environment Brave would give it, runs the file
# writer twice, exercises a folder reset, re-runs bootstrap to prove idempotence, and checks
# the Brave Sync and missing-profile paths. Exit 0 means every check passed.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SB="${1:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/raindrop-rehearsal}"
PY="${RAINDROP_SYNC_PYTHON:-/usr/bin/python3}"
FAILS=0

step()   { printf '\n\033[1m### %s\033[0m\n' "$1"; }
pass()   { printf '  ok    %s\n' "$1"; }
failed() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
expect() { if eval "$2"; then pass "$1"; else failed "$1"; fi; }

rm -rf "$SB"; mkdir -p "$SB/home" "$SB/emptyhome" "$SB/repo"
step "toolchain"
sw_vers -productVersion 2>/dev/null || uname -sr
"$PY" --version; openssl version; bash --version | head -1

step "a copy of the checkout, WITHOUT .env.example (a clone must not need it)"
(cd "$REPO" && tar --exclude=.git --exclude='__pycache__' --exclude='.coverage*' -cf - .) | tar -xf - -C "$SB/repo"
rm -f "$SB/repo/.env.example"
expect "copy has no .env.example" '[ ! -f "$SB/repo/.env.example" ]'

step "a fake Brave layout: Beta channel, last-used profile 'Profile 1'"
BASE="$SB/home/Library/Application Support/BraveSoftware/Brave-Browser-Beta"
PROFILE="$BASE/Profile 1"
mkdir -p "$PROFILE"
printf '{"profile":{"last_used":"Profile 1"}}' > "$BASE/Local State"
"$PY" - "$SB/repo" "$PROFILE/Bookmarks" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[1] + "/src")
import apply_brave
def url(i, name, u):
    return {"date_added": "0", "guid": "00000000-0000-4000-8000-%012d" % i, "id": str(i),
            "name": name, "type": "url", "url": u}
def folder(i, name, kids):
    return {"children": kids, "date_added": "0", "date_modified": "0",
            "guid": "00000000-0000-4000-8000-%012d" % i, "id": str(i), "name": name, "type": "folder"}
doc = {"version": 1, "roots": {
    "bookmark_bar": folder(1, "Bookmarks Bar", [folder(4, "Hand Curated", [url(5, "Existing", "https://www.example.org/")])]),
    "other": folder(2, "Other Bookmarks", []),
    "synced": folder(3, "Mobile Bookmarks", [])}}
doc["checksum"] = apply_brave.checksum(doc)
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=3)
PYEOF
expect "fake profile has a Bookmarks file" '[ -f "$PROFILE/Bookmarks" ]'

# Every knob into the sandbox. HOME too, so a forgotten default still lands inside it.
export HOME="$SB/home"
export RAINDROP_SYNC_ROOT="$SB/root"
export RAINDROP_SYNC_NMH_CHROME="$SB/nmh-chrome"
export RAINDROP_SYNC_NMH_BRAVE="$SB/nmh-brave"
export RAINDROP_SYNC_LAUNCHAGENTS="$SB/agents"
export RAINDROP_SYNC_SKILLS="$SB/skills"
export SKIP_LAUNCHCTL=1
export RAINDROP_SYNC_NO_KEYCHAIN=1
unset RAINDROP_SYNC_PROFILE RAINDROP_TOKEN
ROOT="$SB/root"

step "bootstrap, first run (profile must be discovered, not given)"
if bash "$SB/repo/scripts/bootstrap.sh" </dev/null > "$SB/bootstrap1.log" 2>&1; then
  pass "bootstrap exit 0"
else
  failed "bootstrap exit $? (see below)"; cat "$SB/bootstrap1.log"
fi
expect "bootstrap printed the discovered profile" 'grep -q "Brave profile: $PROFILE" "$SB/bootstrap1.log"'
expect "nothing landed outside the sandbox home's expected places" \
  '[ -z "$(find "$SB/home" -type f -not -path "$SB/home/Library/Application Support/BraveSoftware/*" -not -path "$SB/home/Library/Caches/*" -not -path "$SB/home/.claude/*" 2>/dev/null)" ]'

step "what bootstrap wrote"
expect "config.json records the discovered profile" \
  '"$PY" -c "import json,sys; c=json.load(open(sys.argv[1])); sys.exit(0 if c[\"profile\"]==sys.argv[2] and c[\"brave_sync\"] is False and len(c[\"extension_id\"])==32 else 1)" "$ROOT/config.json" "$PROFILE"'
EXT_ID="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["extension_id"])' "$ROOT/config.json")"
expect "wrapper bin/native_host is executable and pins the sandbox root" \
  '[ -x "$ROOT/bin/native_host" ] && grep -q "RAINDROP_SYNC_ROOT=\"$ROOT\"" "$ROOT/bin/native_host"'
for d in "$SB/nmh-chrome" "$SB/nmh-brave"; do
  expect "host manifest in $(basename "$d") points at the wrapper and allows $EXT_ID" \
    '"$PY" -c "import json,sys; m=json.load(open(sys.argv[1])); sys.exit(0 if m[\"path\"]==sys.argv[2] and m[\"name\"]==\"com.raindrop_sync.host\" and \"chrome-extension://%s/\"%sys.argv[3] in m[\"allowed_origins\"] else 1)" "$d/com.raindrop_sync.host.json" "$ROOT/bin/native_host" "$EXT_ID"'
done
expect "plist carries RAINDROP_SYNC_ROOT and RAINDROP_SYNC_PROFILE" \
  '"$PY" -c "import plistlib,sys; p=plistlib.load(open(sys.argv[1],\"rb\")); e=p[\"EnvironmentVariables\"]; sys.exit(0 if e[\"RAINDROP_SYNC_ROOT\"]==sys.argv[2] and e[\"RAINDROP_SYNC_PROFILE\"]==sys.argv[3] and p[\"Label\"]==\"com.raindrop-sync.apply\" else 1)" "$SB/agents/com.raindrop-sync.apply.plist" "$ROOT" "$PROFILE"'
expect "state.db has the four ledger tables" \
  '"$PY" -c "import sqlite3,sys; t={r[0] for r in sqlite3.connect(sys.argv[1]).execute(\"select name from sqlite_master where type=\x27table\x27\")}; sys.exit(0 if {\"bookmarks\",\"meta\",\"runs\",\"extension_applied\"}<=t else 1)" "$ROOT/state.db"'
expect ".env exists, mode 600, empty token" \
  '[ "$(cat "$ROOT/.env")" = "RAINDROP_TOKEN=" ] && [ "$(stat -f %Lp "$ROOT/.env" 2>/dev/null || stat -c %a "$ROOT/.env")" = "600" ]'
expect "extension manifest carries the pinned key" 'grep -q "\"key\"" "$ROOT/extension/manifest.json"'
expect "skill installed into the sandbox skills dir" '[ -f "$SB/skills/raindrop-sync/SKILL.md" ]'

step "doctor"
if "$ROOT/bin/doctor.sh" > "$SB/doctor1.log" 2>&1; then pass "doctor exit 0"; else failed "doctor exit $?"; fi
expect "doctor reports no FAIL" '! grep -q "^FAIL" "$SB/doctor1.log"'
expect "doctor warns that the token is missing" 'grep -q "token: missing" "$SB/doctor1.log"'
cat "$SB/doctor1.log" | sed 's/^/    /'
FAKE="fake-rehearsal-token-value-0123456789"
printf 'RAINDROP_TOKEN=%s\n' "$FAKE" > "$ROOT/.env"
"$ROOT/bin/doctor.sh" > "$SB/doctor2.log" 2>&1 || true
expect "doctor sees the token" 'grep -q "token: found (${#FAKE} bytes)" "$SB/doctor2.log"'
expect "doctor never prints the token value" '! grep -q "$FAKE" "$SB/doctor2.log"'
printf 'RAINDROP_TOKEN=\n' > "$ROOT/.env"

step "the host through the wrapper, with the environment Brave gives it"
"$PY" - "$ROOT" "$SB/home" <<'PYEOF' > "$SB/host.log" 2>&1 || true
import json, os, struct, subprocess, sys
root, home = sys.argv[1], sys.argv[2]
def talk(msgs):
    data = b"".join(struct.pack("@I", len(m)) + m for m in (json.dumps(x).encode() for x in msgs))
    p = subprocess.run([root + "/bin/native_host"], input=data, capture_output=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": home}, timeout=60)
    out, replies = p.stdout, []
    while len(out) >= 4:
        (n,) = struct.unpack("@I", out[:4]); replies.append(json.loads(out[4:4 + n])); out = out[4 + n:]
    return replies
items = [{"raindrop_id": 1, "name": "One", "url": "https://one.example/", "folder_path": "Raindrop/A"},
         {"raindrop_id": 2, "name": "Two", "url": "https://two.example/", "folder_path": "Raindrop/A"},
         {"raindrop_id": 3, "name": "Three", "url": "https://three.example/", "folder_path": "Raindrop/A"}]
with open(root + "/desired.json", "w") as f: json.dump(items, f)
ping, pending, applied = talk([{"op": "ping"}, {"op": "pending", "client": "brave"},
                              {"op": "applied", "client": "brave",
                               "results": [{"raindrop_id": i["raindrop_id"], "status": "created", "bookmark_id": str(i["raindrop_id"])} for i in items]}])
assert ping.get("ok") is True, ping
assert [i["raindrop_id"] for i in pending["items"]] == [1, 2, 3], pending
assert applied == {"ok": True, "recorded": 3}, applied
assert os.path.exists(root + "/log/native_host.log"), "log must land under the sandbox root (wrapper exports ROOT)"
# reset: the same reply must re-offer everything under the folder; applied consumes the file
with open(root + "/reset_folders.json", "w") as f: json.dump(["Raindrop"], f)
(pend2,) = talk([{"op": "pending", "client": "brave"}])
assert pend2.get("reset_folders") == ["Raindrop"] and [i["raindrop_id"] for i in pend2["items"]] == [1, 2, 3], pend2
talk([{"op": "applied", "client": "brave",
       "results": [{"raindrop_id": i["raindrop_id"], "status": "created", "bookmark_id": str(i["raindrop_id"])} for i in items]}])
(pend3,) = talk([{"op": "pending", "client": "brave"}])
assert pend3["items"] == [] and not pend3.get("reset_folders"), pend3
assert not os.path.exists(root + "/reset_folders.json"), "reset must be consumed"
print("HOST OK")
PYEOF
expect "ping / pending / applied / reset all behaved" 'grep -q "^HOST OK" "$SB/host.log"'
grep -q "^HOST OK" "$SB/host.log" || cat "$SB/host.log"

step "the file writer, reading the profile from config.json"
"$PY" - "$ROOT" <<'PYEOF'
import json, sys
items = [{"raindrop_id": 11, "name": "R1", "url": "https://r1.example/", "folder_path": "Raindrop/Research"},
         {"raindrop_id": 12, "name": "R2", "url": "https://r2.example/", "folder_path": "Bookmarks Bar/Raindrop/Research"},
         {"raindrop_id": 13, "name": "Recipe", "url": "https://recipe.example/", "folder_path": "Raindrop/Recipes"},
         {"raindrop_id": 14, "name": "Dup", "url": "https://WWW.Example.org", "folder_path": "Raindrop/Sites"},
         {"raindrop_id": 15, "name": "Broken", "url": "not a url", "folder_path": "Raindrop/Sites"}]
with open(sys.argv[1] + "/desired.json", "w") as f: json.dump(items, f)
PYEOF
"$PY" "$ROOT/bin/apply_brave.py" > "$SB/apply1.log" 2>&1; rc1=$?
"$PY" "$ROOT/bin/apply_brave.py" > "$SB/apply2.log" 2>&1; rc2=$?
expect "first run exit 0" '[ "$rc1" = 0 ]'
expect "first run skipped the broken url with one line" 'grep -q "skipping 15: invalid url" "$SB/apply1.log"'
expect "first run applied 3 and saw the canonical duplicate as present" 'grep -q "3 to apply (1 already present)" "$SB/apply1.log"'
expect "second run exit 0 with no write needed" '[ "$rc2" = 0 ] && grep -q "no write needed" "$SB/apply2.log"'
expect "nodes landed where they should, checksum valid, no duplicate, backup taken" \
  '"$PY" - "$PROFILE/Bookmarks" "$SB/repo" "$ROOT" <<'"'"'PYEOF'"'"'
import json, os, sys
sys.path.insert(0, sys.argv[2] + "/src"); import apply_brave
doc = json.load(open(sys.argv[1]))
assert apply_brave.checksum(doc) == doc["checksum"]
paths = {}
def walk(n, p):
    q = p + "/" + n["name"]
    if n.get("url"): paths.setdefault(n["url"], []).append(q)
    for c in n.get("children", []): walk(c, q)
walk(doc["roots"]["bookmark_bar"], "")
assert paths["https://r1.example/"] == ["/Bookmarks Bar/Raindrop/Research/R1"], paths
assert paths["https://r2.example/"] == ["/Bookmarks Bar/Raindrop/Research/R2"], paths
assert paths["https://recipe.example/"] == ["/Bookmarks Bar/Raindrop/Recipes/Recipe"], paths
assert paths["https://www.example.org/"] == ["/Bookmarks Bar/Hand Curated/Existing"], paths
assert "https://WWW.Example.org" not in paths and "not a url" not in paths
assert os.listdir(sys.argv[3] + "/backups")
PYEOF'
grep -q "3 to apply" "$SB/apply1.log" || { cat "$SB/apply1.log"; cat "$SB/apply2.log"; }

step "bootstrap, second run (idempotent)"
KEYSUM="$(shasum -a 256 "$ROOT/key.pem" | cut -c1-16)"
bash "$SB/repo/scripts/bootstrap.sh" </dev/null > "$SB/bootstrap2.log" 2>&1 && pass "re-run exit 0" || failed "re-run exit $?"
expect "extension id unchanged" 'grep -q "Extension id: $EXT_ID" "$SB/bootstrap2.log"'
expect "key.pem untouched" '[ "$(shasum -a 256 "$ROOT/key.pem" | cut -c1-16)" = "$KEYSUM" ]'

step "Brave Sync on: no agent, brave_sync recorded, extension path kept"
"$PY" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["sync_metadata"]="x"; json.dump(d, open(p,"w"))' "$PROFILE/Bookmarks"
RAINDROP_SYNC_ROOT="$SB/root-sync" RAINDROP_SYNC_LAUNCHAGENTS="$SB/agents-sync" \
  bash "$SB/repo/scripts/bootstrap.sh" </dev/null > "$SB/bootstrap-sync.log" 2>&1 && pass "bootstrap exit 0 with sync on" || failed "bootstrap failed with sync on"
expect "warned about Brave Sync" 'grep -q "Brave Sync is ON" "$SB/bootstrap-sync.log"'
expect "no plist written" '[ ! -f "$SB/agents-sync/com.raindrop-sync.apply.plist" ]'
expect "config.json brave_sync true" '"$PY" -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))[\"brave_sync\"] is True else 1)" "$SB/root-sync/config.json"'
"$PY" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d.pop("sync_metadata"); json.dump(d, open(p,"w"))' "$PROFILE/Bookmarks"

step "no Brave at all: a clear refusal, then RAINDROP_SYNC_SKIP_PROFILE_CHECK=1"
if HOME="$SB/emptyhome" RAINDROP_SYNC_ROOT="$SB/root-none" bash "$SB/repo/scripts/bootstrap.sh" </dev/null > "$SB/bootstrap-none.log" 2>&1; then
  failed "bootstrap should exit non-zero without a profile"
else
  pass "bootstrap refused (exit $?)"
fi
expect "refusal names the fix" 'grep -q "set RAINDROP_SYNC_PROFILE" "$SB/bootstrap-none.log"'
expect "nothing was created" '[ ! -d "$SB/root-none" ]'
HOME="$SB/emptyhome" RAINDROP_SYNC_ROOT="$SB/root-none" RAINDROP_SYNC_SKIP_PROFILE_CHECK=1 \
  bash "$SB/repo/scripts/bootstrap.sh" </dev/null > "$SB/bootstrap-skip.log" 2>&1 && pass "bootstrap exit 0 with the check skipped" || failed "bootstrap failed with the check skipped"
expect "profile recorded as null" '"$PY" -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))[\"profile\"] is None else 1)" "$SB/root-none/config.json"'
expect "doctor warns (not FAIL) about the missing profile" \
  'HOME="$SB/emptyhome" RAINDROP_SYNC_ROOT="$SB/root-none" RAINDROP_SYNC_LAUNCHAGENTS="$SB/agents" RAINDROP_SYNC_SKIP_PROFILE_CHECK=1 "$SB/root-none/bin/doctor.sh" > "$SB/doctor-none.log" 2>&1; grep -q "^warn  no Brave profile recorded" "$SB/doctor-none.log"'

printf '\n'
if [ "$FAILS" -eq 0 ]; then
  printf '\033[1;32mREHEARSAL PASSED\033[0m (sandbox: %s)\n' "$SB"
else
  printf '\033[1;31mREHEARSAL FAILED: %d check(s)\033[0m (sandbox kept at %s)\n' "$FAILS" "$SB"
  exit 1
fi
