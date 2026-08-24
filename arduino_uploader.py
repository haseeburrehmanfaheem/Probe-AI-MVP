import subprocess
from pathlib import Path

class ArduinoUploader:
    def __init__(self, fqbn: str, port: str):
        self.fqbn=fqbn; self.port=port
    def compile(self, sketch_dir: Path):
        return subprocess.run(["arduino-cli","compile","--fqbn",self.fqbn,str(sketch_dir)],capture_output=True,text=True,shell=False)
    def upload(self, sketch_dir: Path):
        return subprocess.run(["arduino-cli","upload","-p",self.port,"--fqbn",self.fqbn,str(sketch_dir)],capture_output=True,text=True,shell=False)
