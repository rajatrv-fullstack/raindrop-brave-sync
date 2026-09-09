"""FuzzedDataProvider: atheris in CI, a small shim for local smoke runs without libFuzzer.

The shim gives each target the same API surface it uses from atheris so the harness can be
exercised on a machine where atheris does not build (macOS without an LLVM libFuzzer).
"""
import os
import struct

try:  # pragma: no cover - CI path
    import atheris  # type: ignore
    FuzzedDataProvider = atheris.FuzzedDataProvider
    HAVE_ATHERIS = True
except ImportError:  # pragma: no cover - local smoke path
    HAVE_ATHERIS = False

    class FuzzedDataProvider:
        def __init__(self, data):
            self._d = bytearray(data)

        def _take(self, n):
            out = bytes(self._d[:n]); del self._d[:n]; return out

        def ConsumeBool(self):
            b = self._take(1); return bool(b and b[0] & 1)

        def ConsumeIntInRange(self, lo, hi):
            span = hi - lo + 1
            if span <= 1: return lo
            raw = self._take(4)
            v = struct.unpack("<I", raw.ljust(4, b"\0"))[0] if raw else 0
            return lo + (v % span)

        def ConsumeInt(self, nbytes):
            raw = self._take(nbytes)
            return int.from_bytes(raw.ljust(nbytes, b"\0"), "little", signed=True) if raw else 0

        def ConsumeBytes(self, n):
            return self._take(n)

        def ConsumeUnicode(self, n):
            # Mirror atheris: surrogates and every plane are fair game.
            raw = self._take(n * 2)
            raw = raw[: len(raw) - (len(raw) % 2)]  # a trailing odd byte is not a code unit
            return raw.decode("utf-16-le", errors="surrogatepass") if raw else ""

        def ConsumeUnicodeNoSurrogates(self, n):
            return "".join(ch for ch in self.ConsumeUnicode(n) if not 0xD800 <= ord(ch) <= 0xDFFF)

        def remaining_bytes(self):
            return len(self._d)


def run(target, argv, iterations=2000):
    """atheris.Fuzz() when available; otherwise a random smoke loop of the same target."""
    if HAVE_ATHERIS:  # pragma: no cover
        atheris.Setup(argv, target)
        atheris.Fuzz()
        return
    for _ in range(iterations):
        target(os.urandom(int.from_bytes(os.urandom(2), "little") % 512))
    print(f"smoke: {iterations} random inputs, no contract violation")
