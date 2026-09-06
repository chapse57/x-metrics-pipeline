"""Convert the collector's Playwright recording(s) to a README-sized GIF using the ffmpeg that
Playwright already installed (no extra dependency).

    python tools/webm_to_gif.py docs/recording            # -> docs/recording/collect.gif
    python tools/webm_to_gif.py docs/recording --fps 6 --width 960 --speed 2
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
from pathlib import Path


def playwright_ffmpeg() -> str:
    cands = []
    home = Path.home()
    for base in (Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")), home / "AppData/Local/ms-playwright", home / ".cache/ms-playwright", home / "Library/Caches/ms-playwright"):
        if base and base.exists():
            cands += glob.glob(str(base / "ffmpeg-*" / "ffmpeg*"))
    cands = [c for c in cands if not c.endswith(".txt")]
    if not cands:
        sys.exit("Playwright ffmpeg not found — run `python -m playwright install chromium` (it bundles ffmpeg)")
    return sorted(cands)[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir"); ap.add_argument("--out", default=None); ap.add_argument("--fps", type=int, default=6)
    ap.add_argument("--width", type=int, default=800); ap.add_argument("--speed", type=float, default=2.0)
    a = ap.parse_args()
    vids = sorted(Path(a.dir).glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not vids:
        sys.exit(f"no .webm in {a.dir}")
    src = max(vids, key=lambda p: p.stat().st_size)  # the real page recording (a tiny one is the blank first tab)
    out = Path(a.out or Path(a.dir) / "collect.gif")
    ff = playwright_ffmpeg()
    vf = f"setpts=PTS/{a.speed},fps={a.fps},scale={a.width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=96[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, "-loop", "0", str(out)], check=True)
    print(f"{out} ({out.stat().st_size // 1024} KB) from {src.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
