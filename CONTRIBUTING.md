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

**Never test against your live browser profile.** Copy it first:

```bash
LAB=/tmp/lab; rm -rf "$LAB"; mkdir -p "$LAB/Default"
cp ~/Library/Application\ Support/BraveSoftware/Brave-Browser/Default/Bookmarks "$LAB/Default/"
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --user-data-dir="$LAB" --no-first-run --enable-logging=stderr --v=0
```

Every path in the applier and the installer is overridable so tests cannot reach real data:

```bash
RAINDROP_SYNC_ROOT=/tmp/root \
RAINDROP_SYNC_NMH_CHROME=/tmp/nmh \
RAINDROP_SYNC_LAUNCHAGENTS=/tmp/agents \
SKIP_LAUNCHCTL=1 bash scripts/bootstrap.sh
```

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

`fuzz/` holds two atheris targets that CI runs on every push touching `src/`. If you change a
parser or a contract they check (`native_host.handle`, `apply_brave.checksum`,
`apply_brave.preflight`), run the matching target for a minute before opening the pull request;
without libFuzzer on your machine the same file runs a short random smoke loop instead. A
crashing input from CI is uploaded as a workflow artifact and replays with
`python3 fuzz/<target>.py crash-<hash>`. Details in [fuzz/README.md](fuzz/README.md).

## Code

- Python: stdlib only. No dependencies is a feature; keep it.
- Shell: `bash -n` clean, `set -euo pipefail`.
- The extension: plain MV3, no build step. Bump `manifest.json` version when `sw.js` changes - unpacked extensions do not hot-reload, and a stale worker is an afternoon of confusion.

## Things that will be declined

- **Anything that deletes.** Additive-only is the design, not an oversight.
- **A catch-all category.** "Misc", "Other", "Uncategorised" - see the first design principle.
  If the classifier cannot place something, that is information about a gap in the taxonomy.
- **Safari support.** Seven independent avenues are closed. See RESEARCH.md before spending an
  evening on it.
- **Packing a `.crx`.** It permanently poisons the extension id. Finding 3.
