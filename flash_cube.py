#!/usr/bin/env python3
"""Standalone STM32CubeMX/CubeIDE project flasher.

Takes a CubeMX project (a folder containing a .ioc, or the .ioc itself),
optionally regenerates the HAL source from the .ioc with STM32CubeMX headless,
builds it (CMake or Makefile), and flashes the resulting .elf onto an STM32
Nucleo with STM32CubeProgrammer's CLI over SWD (built-in ST-LINK, plain USB).

Usage:
    python3 flash_cube.py path/to/project_dir
    python3 flash_cube.py path/to/project.ioc
    python3 flash_cube.py path/to/project_dir --regen          # regenerate code from .ioc first
    python3 flash_cube.py path/to/project_dir --build-only      # build, don't flash
    python3 flash_cube.py path/to/project_dir --config cube_config.json

Config JSON (all optional):
    {
      "port": "SWD",
      "programmer_cli": "STM32_Programmer_CLI",
      "cubemx_cli": "STM32CubeMX",
      "build_system": "auto",          # auto | cmake | make
      "cmake_build_type": "Debug",
      "cmake_generator": "Ninja",      # Ninja | "Unix Makefiles"
      "jobs": 4
    }
If --config is omitted, it looks for cube_config.json in the project dir, then
next to this script.

Prerequisites (see notes at bottom of this file):
    arm-none-eabi-gcc toolchain, cmake + ninja (or make), STM32CubeProgrammer,
    and (only for --regen) STM32CubeMX.
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
    "port": "SWD",
    "programmer_cli": "STM32_Programmer_CLI",
    "cubemx_cli": "STM32CubeMX",
    "build_system": "auto",      # auto | cmake | make
    "cmake_build_type": "Debug",
    "cmake_generator": "Ninja",
    "jobs": 4,
}


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    print("$ " + " ".join(cmd) + (f"   (in {cwd})" if cwd else ""))
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, shell=False)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    return proc


def resolve_project(target: Path) -> tuple[Path, Path]:
    """Return (project_dir, ioc_file) from a folder or a .ioc path."""
    target = target.expanduser().resolve()
    if target.is_file() and target.suffix == ".ioc":
        return target.parent, target
    if target.is_dir():
        iocs = list(target.glob("*.ioc"))
        if not iocs:
            die(f"no .ioc file found in {target}")
        if len(iocs) > 1:
            die(f"multiple .ioc files in {target}; pass the specific .ioc path")
        return target, iocs[0]
    die(f"not a project dir or .ioc file: {target}")


def load_config(project_dir: Path, explicit: Path | None) -> dict:
    candidates = []
    if explicit:
        candidates.append(explicit)
    else:
        candidates.append(project_dir / "cube_config.json")
        candidates.append(Path(__file__).parent / "cube_config.json")
    for c in candidates:
        if c and c.is_file():
            print(f"Using config: {c}")
            return {**DEFAULT_CONFIG, **json.loads(c.read_text())}
    print("No config file found; using built-in defaults.")
    return dict(DEFAULT_CONFIG)


def regenerate(ioc: Path, cubemx_cli: str):
    """Run STM32CubeMX headless to regenerate code from the .ioc."""
    if not shutil.which(cubemx_cli):
        die(f"{cubemx_cli} not found on PATH (needed for --regen).")
    # CubeMX headless takes a script file of commands.
    script = f"config load {ioc}\nproject generate\nexit\n"
    with tempfile.NamedTemporaryFile("w", suffix=".cubemx", delete=False) as f:
        f.write(script)
        script_path = f.name
    print(f"\n== Regenerating code from {ioc.name} ==")
    r = run([cubemx_cli, "-q", script_path])
    Path(script_path).unlink(missing_ok=True)
    if r.returncode != 0:
        die("CubeMX code generation failed.", r.returncode)


def detect_build_system(project_dir: Path, choice: str) -> str:
    if choice in ("cmake", "make"):
        return choice
    if (project_dir / "CMakeLists.txt").is_file():
        return "cmake"
    # classic CubeMX Makefile projects put the Makefile at the root
    if (project_dir / "Makefile").is_file():
        return "make"
    die(f"could not detect a build system in {project_dir} "
        "(no CMakeLists.txt or Makefile). Set 'build_system' in config.")


def build_cmake(project_dir: Path, cfg: dict) -> Path:
    build_dir = project_dir / "build"
    gen = cfg["cmake_generator"]
    if gen == "Ninja" and not shutil.which("ninja"):
        die("cmake_generator is 'Ninja' but ninja is not on PATH (brew install ninja) "
            "or set cmake_generator to 'Unix Makefiles'.")
    if not shutil.which("cmake"):
        die("cmake not found on PATH (brew install cmake).")
    c = run(["cmake", "-B", str(build_dir), "-G", gen,
             f"-DCMAKE_BUILD_TYPE={cfg['cmake_build_type']}"], cwd=project_dir)
    if c.returncode != 0:
        die("cmake configure failed.", c.returncode)
    b = run(["cmake", "--build", str(build_dir), "-j", str(cfg["jobs"])], cwd=project_dir)
    if b.returncode != 0:
        die("cmake build failed.", b.returncode)
    return build_dir


def build_make(project_dir: Path, cfg: dict) -> Path:
    if not shutil.which("make"):
        die("make not found on PATH.")
    b = run(["make", f"-j{cfg['jobs']}"], cwd=project_dir)
    if b.returncode != 0:
        die("make build failed.", b.returncode)
    # classic CubeMX Makefile emits into ./build
    return project_dir / "build"


def find_elf(search_root: Path) -> Path:
    elfs = list(search_root.rglob("*.elf"))
    if not elfs:
        die(f"build succeeded but no .elf found under {search_root}")
    # pick the most recently modified .elf
    return max(elfs, key=lambda p: p.stat().st_mtime)


def main():
    ap = argparse.ArgumentParser(description="Regenerate, build, and flash an STM32CubeMX project.")
    ap.add_argument("target", type=Path, help="Project directory or .ioc file")
    ap.add_argument("--config", type=Path, default=None, help="Path to cube_config.json")
    ap.add_argument("--regen", action="store_true", help="Regenerate code from .ioc with STM32CubeMX first")
    ap.add_argument("--build-only", action="store_true", help="Build but do not flash")
    args = ap.parse_args()

    project_dir, ioc = resolve_project(args.target)
    print(f"Project: {project_dir}\n.ioc:    {ioc.name}")
    cfg = load_config(project_dir, args.config.expanduser().resolve() if args.config else None)

    if args.regen:
        regenerate(ioc, cfg["cubemx_cli"])

    system = detect_build_system(project_dir, cfg["build_system"])
    print(f"\n== Building with {system} ==")
    build_dir = build_cmake(project_dir, cfg) if system == "cmake" else build_make(project_dir, cfg)

    elf = find_elf(build_dir)
    print(f"Built: {elf}")

    if args.build_only:
        print("\n--build-only set; skipping flash.")
        return

    programmer = cfg["programmer_cli"]
    if not shutil.which(programmer):
        die(f"{programmer} not found on PATH. Install STM32CubeProgrammer and add its bin to PATH.")

    print(f"\n== Flashing over {cfg['port']} ==")
    f = run([programmer, "-c", f"port={cfg['port']}", "-w", str(elf), "-v", "-rst"])
    if f.returncode != 0:
        die("flash failed.", f.returncode)

    print("\nDone: project built and flashed successfully.")


if __name__ == "__main__":
    main()
