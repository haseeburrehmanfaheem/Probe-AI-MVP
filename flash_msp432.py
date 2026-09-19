#!/usr/bin/env python3
"""Standalone TI MSP432 flasher.

Compiles an Arduino/Energia .ino sketch with arduino-cli (Energia MSP432 core)
and flashes the resulting .elf onto an MSP432 LaunchPad (e.g. MSP-EXP432P401R)
with TI's DSLite CLI over the board's built-in XDS110 debugger (plain USB
cable, no jumpers needed).

Usage:
    python3 flash_msp432.py path/to/sketch.ino
    python3 flash_msp432.py path/to/sketch.ino --config path/to/msp432_config.json
    python3 flash_msp432.py path/to/sketch.ino --compile-only

Prerequisites:
    - arduino-cli with the Energia MSP432 core installed. Add the Energia board
      index, then install the core:
        arduino-cli config add board_manager.additional_urls \
          https://energia.nu/packages/package_energia_index.json
        arduino-cli core update-index
        arduino-cli core install energia:msp432r
    - TI DSLite (ships with UniFlash or Code Composer Studio). Put its bin dir
      on PATH so `DSLite` (or dslite.sh) resolves. DSLite flashes over XDS110.

Config JSON (all keys optional except board_fqbn):
    {
      "board_fqbn": "energia:msp432r:MSP-EXP432P401R",
      "programmer_cli": "DSLite",
      "ccxml": "MSP432P401R.ccxml"
    }
If "ccxml" is omitted, DSLite auto-detects the connected XDS110 target.
If --config is omitted, it looks for msp432_config.json next to the .ino, then
next to this script.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_CONFIG = {
    "board_fqbn": "energia:msp432r:MSP-EXP432P401R",
    "programmer_cli": "DSLite",
    "ccxml": None,
}


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    return proc


def load_config(ino: Path, explicit: Path | None) -> dict:
    candidates = []
    if explicit:
        candidates.append(explicit)
    else:
        candidates.append(ino.parent / "msp432_config.json")
        candidates.append(Path(__file__).parent / "msp432_config.json")
    for c in candidates:
        if c and c.is_file():
            print(f"Using config: {c}")
            cfg = {**DEFAULT_CONFIG, **json.loads(c.read_text())}
            return cfg
    print("No config file found; using built-in defaults.")
    return dict(DEFAULT_CONFIG)


def prepare_sketch_dir(ino: Path, workdir: Path) -> Path:
    """arduino-cli requires the sketch's parent dir to share the .ino basename."""
    sketch_dir = workdir / ino.stem
    sketch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ino, sketch_dir / f"{ino.stem}.ino")
    return sketch_dir


def main():
    ap = argparse.ArgumentParser(description="Compile and flash an .ino to an MSP432 LaunchPad.")
    ap.add_argument("ino", type=Path, help="Path to the .ino sketch file")
    ap.add_argument("--config", type=Path, default=None, help="Path to msp432_config.json")
    ap.add_argument("--compile-only", action="store_true", help="Build but do not flash")
    args = ap.parse_args()

    ino = args.ino.expanduser().resolve()
    if not ino.is_file() or ino.suffix != ".ino":
        die(f"not an .ino file: {ino}")

    cfg = load_config(ino, args.config.expanduser().resolve() if args.config else None)
    fqbn = cfg["board_fqbn"]
    programmer = cfg["programmer_cli"]
    ccxml = cfg.get("ccxml")

    if not shutil.which("arduino-cli"):
        die("arduino-cli not found on PATH. Install it and the energia:msp432r core.")

    with tempfile.TemporaryDirectory(prefix="msp432_flash_") as tmp:
        workdir = Path(tmp)
        sketch_dir = prepare_sketch_dir(ino, workdir)
        build_dir = sketch_dir / "build"
        build_dir.mkdir()

        print(f"\n== Compiling {ino.name} for {fqbn} ==")
        c = run(["arduino-cli", "compile", "--fqbn", fqbn,
                 "--output-dir", str(build_dir), str(sketch_dir)])
        if c.returncode != 0:
            die("compilation failed.", c.returncode)

        # Energia's MSP432 core emits an ELF; DSLite loads it directly.
        elfs = list(build_dir.glob("*.elf")) or list(build_dir.glob("*.out"))
        if not elfs:
            die("compile succeeded but no .elf/.out was produced.")
        elf = elfs[0]
        print(f"Built: {elf.name}")

        if args.compile_only:
            print("\n--compile-only set; skipping flash.")
            return

        if not shutil.which(programmer):
            die(f"{programmer} not found on PATH. Install TI UniFlash/CCS and add its bin to PATH.")

        print("\n== Flashing over XDS110 ==")
        cmd = [programmer, "flash"]
        if ccxml:
            cmd.append(f"--config={ccxml}")
        cmd += ["--verbose", str(elf)]
        f = run(cmd)
        if f.returncode != 0:
            die("flash failed.", f.returncode)

    print("\nDone: sketch compiled and flashed successfully.")


if __name__ == "__main__":
    main()
