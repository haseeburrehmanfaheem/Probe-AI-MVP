import subprocess
from pathlib import Path


class STM32Uploader:
    """Drop-in analog of ArduinoUploader for STM32 Nucleo boards.

    - compile(): builds the sketch with arduino-cli using the STM32 (STM32duino) core,
      emitting build artifacts (.elf/.bin) into <sketch_dir>/build.
    - upload():  flashes the produced .elf with STM32CubeProgrammer's CLI over SWD,
      using the Nucleo's built-in ST-LINK (plain USB cable, no BOOT0 needed).
    """

    def __init__(self, fqbn: str, port: str = "SWD", programmer_cli: str = "STM32_Programmer_CLI"):
        self.fqbn = fqbn            # e.g. "STMicroelectronics:stm32:Nucleo_64:pnum=NUCLEO_F401RE"
        self.port = port            # "SWD" for Nucleo's built-in ST-LINK
        self.programmer_cli = programmer_cli

    def _build_dir(self, sketch_dir: Path) -> Path:
        return sketch_dir / "build"

    def compile(self, sketch_dir: Path):
        build_dir = self._build_dir(sketch_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            ["arduino-cli", "compile", "--fqbn", self.fqbn,
             "--output-dir", str(build_dir), str(sketch_dir)],
            capture_output=True, text=True, shell=False,
        )

    def _find_elf(self, sketch_dir: Path) -> Path:
        elfs = list(self._build_dir(sketch_dir).glob("*.elf"))
        if not elfs:
            raise FileNotFoundError("No .elf found in build dir; did compile() succeed?")
        return elfs[0]

    def upload(self, sketch_dir: Path):
        elf = self._find_elf(sketch_dir)
        # -c connect over SWD (ST-LINK), -w write firmware, -v verify, -rst reset & run
        return subprocess.run(
            [self.programmer_cli, "-c", f"port={self.port}",
             "-w", str(elf), "-v", "-rst"],
            capture_output=True, text=True, shell=False,
        )
