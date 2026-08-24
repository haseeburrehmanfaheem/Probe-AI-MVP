# WT61PC assistant with Arduino CLI code generation

Flow:
1. Measure/change frequency using constrained host tools.
2. Ask what the user wants next.
3. LLM modifies the known-good base Arduino sketch.
4. Python saves it as `generated/GeneratedSketch/GeneratedSketch.ino`.
5. `arduino-cli compile --fqbn arduino:avr:uno ...` compiles it.
6. If compilation fails once, the compiler error is sent back to the LLM for repair.
7. `arduino-cli upload -p <port> --fqbn arduino:avr:uno ...` uploads it.
8. Successfully uploaded code becomes the next base sketch.

## Setup
```bash
brew install arduino-cli
arduino-cli core update-index
arduino-cli core install arduino:avr
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="YOUR_KEY"
python main.py
```

Close Arduino Serial Monitor before running so the upload process can open the port.
