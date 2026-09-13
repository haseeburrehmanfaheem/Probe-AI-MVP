#!/usr/bin/env python3
"""Standalone STM32 flasher.

Compiles an Arduino .ino sketch with arduino-cli (STM32 core) and flashes the
resulting .elf onto an STM32 Nucleo with STM32CubeProgrammer's CLI over SWD
(the board's built-in ST-LINK — plain USB cable, no BOOT0 jumper needed).

Usage:
    python3 flash_stm32.py path/to/sketch.ino
    python3 flash_stm32.py path/to/sketch.ino --config path/to/stm32_config.json
    python3 flash_stm32.py path/to/sketch.ino --compile-only

Config JSON (all keys optional except board_fqbn):
    {
      "board_fqbn": "STMicroelectronics:stm32:Nucleo_64:pnum=NUCLEO_F401RE",
      "port": "SWD",
      "programmer_cli": "STM32_Programmer_CLI"
    }
If --config is omitted, it looks for stm32_config.json next to the .ino, then
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
    "board_fqbn": "STMicroelectronics:stm32:Nucleo_64:pnum=NUCLEO_F401RE",
    "port": "SWD",
    "programmer_cli": "STM32_Programmer_CLI",
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
        candidates.append(ino.parent / "stm32_config.json")
        candidates.append(Path(__file__).parent / "stm32_config.json")
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
    ap = argparse.ArgumentParser(description="Compile and flash an .ino to an STM32 Nucleo.")
    ap.add_argument("ino", type=Path, help="Path to the .ino sketch file")
    ap.add_argument("--config", type=Path, default=None, help="Path to stm32_config.json")
    ap.add_argument("--compile-only", action="store_true", help="Build but do not flash")
    args = ap.parse_args()

    ino = args.ino.expanduser().resolve()
    if not ino.is_file() or ino.suffix != ".ino":
        die(f"not an .ino file: {ino}")

    cfg = load_config(ino, args.config.expanduser().resolve() if args.config else None)
    fqbn = cfg["board_fqbn"]
    port = cfg["port"]
    programmer = cfg["programmer_cli"]

    if not shutil.which("arduino-cli"):
        die("arduino-cli not found on PATH. Install it and the STMicroelectronics:stm32 core.")

    with tempfile.TemporaryDirectory(prefix="stm32_flash_") as tmp:
        workdir = Path(tmp)
        sketch_dir = prepare_sketch_dir(ino, workdir)
        build_dir = sketch_dir / "build"
        build_dir.mkdir()

        print(f"\n== Compiling {ino.name} for {fqbn} ==")
        c = run(["arduino-cli", "compile", "--fqbn", fqbn,
                 "--output-dir", str(build_dir), str(sketch_dir)])
        if c.returncode != 0:
            die("compilation failed.", c.returncode)

        elfs = list(build_dir.glob("*.elf"))
        if not elfs:
            die("compile succeeded but no .elf was produced.")
        elf = elfs[0]
        print(f"Built: {elf.name}")

        if args.compile_only:
            print("\n--compile-only set; skipping flash.")
            return

        if not shutil.which(programmer):
            die(f"{programmer} not found on PATH. Install STM32CubeProgrammer and add its bin to PATH.")

        print(f"\n== Flashing over {port} ==")
        f = run([programmer, "-c", f"port={port}", "-w", str(elf), "-v", "-rst"])
        if f.returncode != 0:
            die("flash failed.", f.returncode)

    print("\nDone: sketch compiled and flashed successfully.")


if __name__ == "__main__":
    main()
