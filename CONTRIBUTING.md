# Contributing

The most useful contributions are **corrections to [RESEARCH.md](RESEARCH.md)**.

Every claim in that file is labelled *verified*, *source-derived* or *untested*. Findings 5 and
6 in particular were designed against rather than reproduced. If you reproduce one, or find one
that is wrong, that is worth more than a feature.

## Reporting a research correction

Open an issue with:

1. Which finding, by number.
2. What you observed instead.
3. The exact command or test that produced it.
4. Your Brave/Chrome version, macOS version, and whether the browser was running.

Include the raw output. "It didn't work" cannot be acted on; a `launch_context.cc` log line can.

## Testing changes safely

**Never test against your live browser profile.** Copy it first. The installer records the
profile it chose in `~/.raindrop-sync/config.json`; read it from there rather than assuming
`Brave-Browser/Default`:

```bash
P="$(python3 -c 'import json,os; r=os.environ.get("RAINDROP_SYNC_ROOT", os.path.expanduser("~/.raindrop-sync")); print(json.load(open(r + "/config.json"))["profile"])')"
LAB=/tmp/lab; rm -rf "$LAB"; mkdir -p "$LAB/Default"
cp "$P/Bookmarks" "$LAB/Default/"
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --user-data-dir="$LAB" --no-first-run --enable-logging=stderr --v=0
```

Every path the installer, the doctor and the two appliers touch is overridable, but only if you
set **all** of the knobs. Any one left at its default writes to the corresponding real location;
the Brave-side host manifest and the Claude skill directory are the two people forget, and
either one silently breaks a live install on the same machine. The complete sandbox is:

```bash
SB=/tmp/sandbox; rm -rf "$SB"; mkdir -p "$SB/home" "$SB/profile"
HOME="$SB/home" \
RAINDROP_SYNC_ROOT="$SB/root" \
RAINDROP_SYNC_PROFILE="$SB/profile" \
RAINDROP_SYNC_NMH_CHROME="$SB/nmh-chrome" \
RAINDROP_SYNC_NMH_BRAVE="$SB/nmh-brave" \
RAINDROP_SYNC_LAUNCHAGENTS="$SB/agents" \
RAINDROP_SYNC_SKILLS="$SB/skills" \
SKIP_LAUNCHCTL=1 \
RAINDROP_SYNC_SKIP_PROFILE_CHECK=1 \
RAINDROP_SYNC_NO_KEYCHAIN=1 \
bash scripts/bootstrap.sh
```

`HOME` is in the list because every default is derived from it, and because the system Python
writes its bytecode cache under `~/Library/Caches`: with `HOME` redirected, a knob you forgot
still lands inside the sandbox. `RAINDROP_SYNC_SKIP_PROFILE_CHECK=1` lets bootstrap finish
without a `Bookmarks` file in `$SB/profile`; copy one in from the lab above if the change you
are testing is in the writer. Run the doctor with the same environment
(`... bash scripts/doctor.sh`) to see what the sandbox install looks like from the outside, and
the writer with `RAINDROP_SYNC_PROFILE` and `RAINDROP_SYNC_ROOT` pointed at the same places.

**The token is the exception.** `get-token.sh` reads your **real login keychain** unless
`RAINDROP_SYNC_NO_KEYCHAIN=1` is set, no matter where `RAINDROP_SYNC_ROOT` points. A test that
expects "no token" and forgets that knob prints your real token into whatever captured stdout,
and a token that has been printed must be rotated (SETUP.md, step 2). Tests that need a value
set `RAINDROP_TOKEN` to a made-up one. `seed-token.sh` always writes to the real keychain,
under `RAINDROP_SYNC_KEYCHAIN_SERVICE` (default `raindrop-api`), and its `-U` replaces an
existing item: never run it from a test, or point the service name at a throwaway.

This is not optional politeness - a bug in the writer silently destroys a bookmark library, and
Chromium has no restore path. See finding 6.

## Tests

`tests/` is a pytest suite that runs on every push and pull request (see the Tests badge).
It exercises the writer and the native host against throwaway directories only; nothing in it
reads or writes a real profile.

**A change that alters behaviour must come with a test that would have failed before it.** A
pull request that adds functionality without a test, or that weakens an existing test to make
it pass, will be sent back. Run the suite locally with:

```bash
python3 -m pip install "pytest>=9.0.3" coverage   # the suite needs Python 3.10+; the tool itself runs on 3.9+
python3 -m pytest tests -q
```

`tests/js/run.mjs` is a Node harness for the extension's service worker, `extension/sw.js`,
which pytest cannot reach. Run it when `node` is present:

```bash
node tests/js/run.mjs
```

`fuzz/` holds two atheris targets that CI runs on every push touching `src/`. If you change a
parser or a contract they check (`native_host.handle`, `apply_brave.checksum`,
`apply_brave.preflight`), run the matching target for a minute before opening the pull request;
without libFuzzer on your machine the same file runs a short random smoke loop instead. A
crashing input from CI is uploaded as a workflow artifact and replays with
`python3 fuzz/<target>.py crash-<hash>`. Details in [fuzz/README.md](fuzz/README.md).

## Code

- Python: stdlib only. No dependencies is a feature; keep it.
- Python must run on the system interpreter, which is 3.9: no `match` statements, no `X | Y`
  unions at runtime, no parenthesised context managers. Byte-compile with
  `/usr/bin/python3 -m py_compile src/*.py` before opening a pull request.
- Shell: `bash -n` clean, `set -euo pipefail`.
- The extension: plain MV3, no build step. Bump `manifest.json` version when `sw.js` changes - unpacked extensions do not hot-reload, and a stale worker is an afternoon of confusion.
- The ledger schema has one home, `native_host.py` (`--init-db`, and the same statements run
  lazily when the host opens the database). Nothing else creates tables.
- The `folder_path` and URL rules are implemented three times - `native_host.op_pending`,
  `apply_brave.find_folder` and `resolveFolder` in `sw.js` - and must stay identical. A change
  to one is a change to all three, plus the tests for each.
- Prose and comments: no em dashes; a hyphen, a colon or a new sentence instead. British or
  American spelling, either, but consistent within a file. Example categories in documentation
  are invented (Gardening, Cycling, Recipes, Research), never anyone's real ones.

## Things that will be declined

- **Anything that deletes.** Additive-only is the design, not an oversight.
- **A catch-all category.** "Misc", "Other", "Uncategorised" - see the first design principle.
  If the classifier cannot place something, that is information about a gap in the taxonomy.
- **Safari support.** Seven independent avenues are closed. See RESEARCH.md before spending an
  evening on it.
- **Packing a `.crx`.** It permanently poisons the extension id. Finding 3.
