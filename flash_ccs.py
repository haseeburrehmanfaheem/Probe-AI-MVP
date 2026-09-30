#!/usr/bin/env python3
"""Standalone TI MSP432 project flasher (the MSP432 counterpart of flash_cube.py).

Takes a native MSP432 project (SimpleLink MSP432 SDK / driverlib, not an
Energia sketch -- use flash_msp432.py for .ino files), builds it with Make,
CMake, or Code Composer Studio headless, and flashes the resulting .out/.elf
onto an MSP432 LaunchPad over the board's built-in XDS110 debugger (plain USB).

Supported projects (pass the folder or the project file itself):
    - Makefile / makefile at the root, or in a gcc/ subdir (SDK example layout)
    - CMakeLists.txt at the root
    - CCS project (.project + .cproject) -> built with CCS headless
    - CCS .projectspec (SDK examples ship one in ccs/) -> imported + built with CCS headless

Usage:
    python3 flash_ccs.py path/to/project_dir
    python3 flash_ccs.py path/to/blink.projectspec
    python3 flash_ccs.py path/to/project_dir/.project
    python3 flash_ccs.py path/to/project_dir/gcc/makefile
    python3 flash_ccs.py path/to/project_dir --build-only      # build, don't flash
    python3 flash_ccs.py path/to/firmware.out --flash-only     # flash an existing image
    python3 flash_ccs.py path/to/project_dir --config ccs_config.json

Config JSON (all optional):
    {
      "build_system": "auto",          # auto | make | cmake | ccs
      "flasher": "auto",               # auto | openocd | dslite
      "openocd_cli": "openocd",
      "openocd_board": "board/ti_msp432_launchpad.cfg",
      "dslite_cli": "DSLite",
      "ccxml": null,                   # DSLite target config; auto-found in targetConfigs/
      "ccs_cli": null,                 # CCS headless binary (eclipse ccstudio or ccs-server-cli.sh)
      "ccs_configuration": "Debug",
      "make_target": "all",
      "make_vars": {},                 # e.g. {"SIMPLELINK_MSP432P4_SDK_INSTALL_DIR": "/Users/me/ti/simplelink_msp432p4_sdk_3_40_01_02"}
      "cmake_build_type": "Debug",
      "cmake_generator": "Ninja",      # Ninja | "Unix Makefiles"
      "jobs": 4
    }
"auto" flasher uses DSLite when it is on PATH and a .ccxml is available,
otherwise OpenOCD. If --config is omitted, it looks for ccs_config.json in the
project dir, then next to this script.

Prerequisites (see notes at bottom of this file):
    arm-none-eabi-gcc (or the TI compiler, via CCS), make or cmake + ninja,
    and OpenOCD (brew install open-ocd) or TI DSLite (UniFlash/CCS).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = {
    "build_system": "auto",      # auto | make | cmake | ccs
    "flasher": "auto",           # auto | openocd | dslite
    "openocd_cli": "openocd",
    "openocd_board": "board/ti_msp432_launchpad.cfg",
    "dslite_cli": "DSLite",
    "ccxml": None,
    "ccs_cli": None,
    "ccs_configuration": "Debug",
    "make_target": "all",
    "make_vars": {},
    "cmake_build_type": "Debug",
    "cmake_generator": "Ninja",
    "jobs": 4,
}

IMAGE_SUFFIXES = (".out", ".elf", ".axf")
MAKEFILE_NAMES = ("Makefile", "makefile", "GNUmakefile")


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


def load_config(project_dir: Path, explicit: Path | None) -> dict:
    candidates = []
    if explicit:
        candidates.append(explicit)
    else:
        candidates.append(project_dir / "ccs_config.json")
        candidates.append(Path(__file__).parent / "ccs_config.json")
    for c in candidates:
        if c and c.is_file():
            print(f"Using config: {c}")
            return {**DEFAULT_CONFIG, **json.loads(c.read_text())}
    print("No config file found; using built-in defaults.")
    return dict(DEFAULT_CONFIG)


def resolve_project(target: Path) -> tuple[Path, Path | None, str | None]:
    """Return (project_dir, projectspec, forced_build_system) from a folder or project file."""
    if target.is_file():
        if target.suffix == ".projectspec":
            return target.parent, target, "ccs"
        if target.name in (".project", ".cproject"):
            return target.parent, None, "ccs"
        if target.name == "CMakeLists.txt":
            return target.parent, None, "cmake"
        if target.name in MAKEFILE_NAMES:
            # a gcc/ makefile belongs to the example one level up
            d = target.parent
            return (d.parent if d.name == "gcc" else d), None, "make"
        die(f"unsupported project file: {target} "
            "(expected .projectspec, .project, .cproject, CMakeLists.txt, or a Makefile)")
    if target.is_dir():
        return target, None, None
    die(f"not a project dir or project file: {target}")


def find_projectspec(project_dir: Path) -> Path | None:
    specs = list(project_dir.glob("*.projectspec")) or list((project_dir / "ccs").glob("*.projectspec"))
    if len(specs) > 1:
        die(f"multiple .projectspec files in {project_dir}; pass the specific .projectspec path")
    return specs[0] if specs else None


def find_makefile_dir(project_dir: Path) -> Path | None:
    # SDK examples keep the gcc makefile in <example>/gcc/
    for d in (project_dir, project_dir / "gcc"):
        if any((d / n).is_file() for n in MAKEFILE_NAMES):
            return d
    return None


def detect_build_system(project_dir: Path, choice: str) -> str:
    if choice in ("make", "cmake", "ccs"):
        return choice
    if (project_dir / "CMakeLists.txt").is_file():
        return "cmake"
    if find_makefile_dir(project_dir):
        return "make"
    if (project_dir / ".cproject").is_file() and (project_dir / ".project").is_file():
        return "ccs"
    if find_projectspec(project_dir):
        return "ccs"
    die(f"could not detect a build system in {project_dir} "
        "(no CMakeLists.txt, Makefile, CCS .project, or .projectspec). Set 'build_system' in config.")


def build_make(project_dir: Path, cfg: dict) -> Path:
    if not shutil.which("make"):
        die("make not found on PATH.")
    make_dir = find_makefile_dir(project_dir)
    if not make_dir:
        die(f"no Makefile found in {project_dir} or {project_dir / 'gcc'}")
    make_vars = [f"{k}={v}" for k, v in cfg["make_vars"].items()]
    b = run(["make", f"-j{cfg['jobs']}", cfg["make_target"], *make_vars], cwd=make_dir)
    if b.returncode != 0:
        die("make build failed.", b.returncode)
    return make_dir


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


def build_ccs(project_dir: Path, cfg: dict, projectspec: Path | None = None) -> Path:
    """Import + build a CCS project (or .projectspec) headless.

    Uses a scratch workspace at <project_dir>/.ccs_workspace. A .projectspec import
    copies the project into that workspace, so the built image lands there too.
    """
    ccs = cfg["ccs_cli"]
    if not ccs or not (shutil.which(ccs) or Path(ccs).expanduser().is_file()):
        die("CCS project detected but 'ccs_cli' is not set or not found. Point it at "
            "e.g. /Applications/ti/ccs1260/ccs/eclipse/Ccstudio.app/Contents/MacOS/ccstudio "
            "(CCS <= 12) or /Applications/ti/ccs2000/ccs/eclipse/ccs-server-cli.sh (CCS 20+).")
    ccs = str(Path(ccs).expanduser()) if Path(ccs).expanduser().is_file() else ccs
    # CCS 20+ (Theia) renamed the headless application ids.
    if "ccs-server-cli" in Path(ccs).name:
        import_app, build_app, ws_flag = ("com.ti.ccs.apps.importProject",
                                          "com.ti.ccs.apps.buildProject", "-workspace")
    else:
        import_app, build_app, ws_flag = ("com.ti.ccstudio.apps.projectImport",
                                          "com.ti.ccstudio.apps.projectBuild", "-data")

    if projectspec is None and not (project_dir / ".project").is_file():
        projectspec = find_projectspec(project_dir)

    # Eclipse project name comes from .project / the projectspec, not the folder name.
    if projectspec:
        m = re.search(r"<project\b[^>]*\bname=\"([^\"]+)\"", projectspec.read_text())
        name = m.group(1).strip() if m else projectspec.stem
        location = projectspec
    else:
        m = re.search(r"<name>([^<]+)</name>", (project_dir / ".project").read_text())
        name = m.group(1).strip() if m else project_dir.name
        location = project_dir

    ws = project_dir / ".ccs_workspace"
    shutil.rmtree(ws, ignore_errors=True)  # re-importing into a stale workspace fails
    ws.mkdir()
    i = run([ccs, "-noSplash", ws_flag, str(ws), "-application", import_app,
             "-ccs.location", str(location)])
    if i.returncode != 0:
        die("CCS project import failed.", i.returncode)
    b = run([ccs, "-noSplash", ws_flag, str(ws), "-application", build_app,
             "-ccs.projects", name, "-ccs.configuration", cfg["ccs_configuration"]])
    if b.returncode != 0:
        die("CCS build failed.", b.returncode)
    # in-place .project builds go to <project>/<config>; projectspec builds go into ws
    return ws if projectspec else project_dir / cfg["ccs_configuration"]


def find_image(search_root: Path, newer_than: float) -> Path:
    images = [p for s in IMAGE_SUFFIXES for p in search_root.rglob(f"*{s}")]
    fresh = [p for p in images if p.stat().st_mtime >= newer_than]
    if not fresh:
        die(f"build succeeded but no fresh .out/.elf/.axf found under {search_root}")
    return max(fresh, key=lambda p: p.stat().st_mtime)


def find_ccxml(project_dir: Path | None, cfg: dict) -> Path | None:
    if cfg["ccxml"]:
        p = Path(cfg["ccxml"]).expanduser()
        if not p.is_absolute() and project_dir:
            p = project_dir / p
        if not p.is_file():
            die(f"ccxml not found: {p}")
        return p
    if project_dir:
        found = sorted(project_dir.glob("targetConfigs/*.ccxml")) or \
            sorted(project_dir.glob(".ccs_workspace/*/targetConfigs/*.ccxml"))
        if found:
            return found[0]
    return None


def flash(image: Path, project_dir: Path | None, cfg: dict):
    ccxml = find_ccxml(project_dir, cfg)
    flasher = cfg["flasher"]
    if flasher == "auto":
        flasher = "dslite" if ccxml and shutil.which(cfg["dslite_cli"]) else "openocd"

    if flasher == "dslite":
        if not shutil.which(cfg["dslite_cli"]):
            die(f"{cfg['dslite_cli']} not found on PATH. Install TI UniFlash/CCS and add DSLite to PATH.")
        if not ccxml:
            die("DSLite needs a target config: set 'ccxml' in config (UniFlash can generate one "
                "for MSP432P401R + XDS110) or use flasher 'openocd'.")
        print(f"\n== Flashing over XDS110 with DSLite ({ccxml.name}) ==")
        f = run([cfg["dslite_cli"], "flash", "-c", str(ccxml), "-f", "-v", str(image)])
    elif flasher == "openocd":
        if not shutil.which(cfg["openocd_cli"]):
            die(f"{cfg['openocd_cli']} not found on PATH (brew install open-ocd).")
        print(f"\n== Flashing over XDS110 with OpenOCD ({cfg['openocd_board']}) ==")
        f = run([cfg["openocd_cli"], "-f", cfg["openocd_board"],
                 "-c", f"program {{{image}}} verify reset exit"])
    else:
        die(f"unknown flasher '{flasher}' (expected auto | openocd | dslite)")

    if f.returncode != 0:
        die("flash failed.", f.returncode)


def main():
    ap = argparse.ArgumentParser(description="Build and flash a native TI MSP432 project.")
    ap.add_argument("target", type=Path,
                    help="Project directory or project file (.projectspec, .project, Makefile, "
                         "CMakeLists.txt), or a .out/.elf with --flash-only")
    ap.add_argument("--config", type=Path, default=None, help="Path to ccs_config.json")
    ap.add_argument("--build-only", action="store_true", help="Build but do not flash")
    ap.add_argument("--flash-only", action="store_true", help="Flash an existing .out/.elf, skip building")
    args = ap.parse_args()

    target = args.target.expanduser().resolve()
    explicit_cfg = args.config.expanduser().resolve() if args.config else None

    if args.flash_only:
        if not target.is_file() or target.suffix not in IMAGE_SUFFIXES:
            die(f"--flash-only expects a .out/.elf/.axf file: {target}")
        project_dir = target.parent
        cfg = load_config(project_dir, explicit_cfg)
        flash(target, None, cfg)
        print("\nDone: image flashed successfully.")
        return

    project_dir, projectspec, forced = resolve_project(target)
    print(f"Project: {project_dir}" + (f"\nSpec:    {projectspec.name}" if projectspec else ""))
    cfg = load_config(project_dir, explicit_cfg)

    system = forced or detect_build_system(project_dir, cfg["build_system"])
    print(f"\n== Building with {system} ==")
    started = time.time() - 1  # tolerate coarse filesystem mtimes
    if system == "ccs":
        out_dir = build_ccs(project_dir, cfg, projectspec)
    else:
        out_dir = {"make": build_make, "cmake": build_cmake}[system](project_dir, cfg)

    image = find_image(out_dir, started)
    print(f"Built: {image}")

    if args.build_only:
        print("\n--build-only set; skipping flash.")
        return

    flash(image, project_dir, cfg)
    print("\nDone: project built and flashed successfully.")


if __name__ == "__main__":
    main()

# Setup notes (macOS):
#   brew install --cask gcc-arm-embedded      # arm-none-eabi-gcc
#   brew install open-ocd make cmake ninja
#   SimpleLink MSP432P4 SDK: https://www.ti.com/tool/SIMPLELINK-MSP432-SDK
#     SDK gcc makefiles read SIMPLELINK_MSP432P4_SDK_INSTALL_DIR (and GCC_ARMCOMPILER)
#     from imports.mak -- set them there or via "make_vars" in ccs_config.json.
#   Optional: TI UniFlash (DSLite) or Code Composer Studio for CCS projects / TI compiler.
#   If the XDS110 firmware is old, OpenOCD may refuse it; update with xdsdfu from UniFlash/CCS.
