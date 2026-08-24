import os, shutil
from pathlib import Path
from openai import OpenAI
from pydantic import BaseModel
from arduino_uploader import ArduinoUploader

class GeneratedSketch(BaseModel):
    code: str
    summary: str

class ArduinoCodeAgent:
    def __init__(self, config, base_sketch_path="arduino/base_sketch/base_sketch.ino", prompt_path="prompts/codegen_prompt.txt"):
        self.config=config
        self.client=OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        self.model=os.environ.get("OPENAI_MODEL","gpt-5.4-mini-2026-03-17")
        self.base_sketch_path=Path(base_sketch_path)
        self.prompt_template=Path(prompt_path).read_text()
        self.uploader=ArduinoUploader(config["board_fqbn"],config["port"])

    def _generate(self, user_request, base_code):
        prompt=self.prompt_template.format(base_sketch=base_code,user_request=user_request)
        r=self.client.responses.parse(model=self.model,input=prompt,text_format=GeneratedSketch)
        return r.output_parsed

    def _repair(self,user_request,code,compiler_error):
        r=self.client.responses.parse(model=self.model,input=f"""Repair this complete Arduino UNO sketch. Preserve the WT61PC wiring and requested behavior.

USER REQUEST:
{user_request}

SKETCH:
{code}

COMPILER ERROR:
{compiler_error}
""",text_format=GeneratedSketch)
        return r.output_parsed

    def generate_compile_upload(self,user_request):
        base_code=self.base_sketch_path.read_text()
        g=self._generate(user_request,base_code)
        print("\nLLM plan:",g.summary)
        code=g.code
        max_repairs=self.config.get("max_codegen_repairs",1)
        for attempt in range(max_repairs+1):
            sketch_dir=Path("generated")/"GeneratedSketch"
            if sketch_dir.exists(): shutil.rmtree(sketch_dir)
            sketch_dir.mkdir(parents=True)
            sketch_file=sketch_dir/"GeneratedSketch.ino"
            sketch_file.write_text(code)
            print(f"\nCompiling generated sketch (attempt {attempt+1})...")
            result=self.uploader.compile(sketch_dir)
            if result.stdout.strip(): print(result.stdout.strip())
            if result.stderr.strip(): print(result.stderr.strip())
            if result.returncode==0: break
            if attempt>=max_repairs: raise RuntimeError("Generated sketch did not compile after repair attempts.")
            print("Compile failed. Asking LLM to repair it...")
            code=self._repair(user_request,code,(result.stdout+"\n"+result.stderr)[-12000:]).code
        print("\nCompilation succeeded. Uploading to Arduino...")
        upload=self.uploader.upload(sketch_dir)
        if upload.stdout.strip(): print(upload.stdout.strip())
        if upload.stderr.strip(): print(upload.stderr.strip())
        if upload.returncode!=0: raise RuntimeError("Arduino upload failed.")
        self.base_sketch_path.write_text(code)
        print("\nUpload succeeded.")
        return sketch_file
