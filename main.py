import json
from code_agent import ArduinoCodeAgent
from llm_assistant import Action, FrequencyAssistant
from sensor_controller import WT61PCController

def main():
    print("\n======================================\n WT61PC ASSISTANT\n======================================\n")
    config=json.load(open("config.json"))
    controller=WT61PCController(); assistant=FrequencyAssistant(controller.allowed_rates)
    try:
        print("Connecting to Arduino..."); controller.connect()
        print("Measuring current WT61PC frequency..."); current_hz=controller.measure_frequency()
        print("\nAssistant:",assistant.opening_message(current_hz))
        while True:
            user_reply=input("You: ").strip()
            if not user_reply: continue
            intent=assistant.interpret_reply(user_reply,current_hz)
            if intent.action==Action.no_change: print("Assistant:",intent.message); break
            if intent.action==Action.clarify: print("Assistant:",intent.message); continue
            target_hz=intent.target_hz
            if controller.verify_frequency(target_hz,current_hz): print("Assistant:",f"Already at about {current_hz:.2f} Hz."); break
            print(f"\nChanging sensor output rate to {target_hz} Hz...")
            controller.set_output_rate(target_hz)
            measured_hz=controller.measure_frequency()
            verified=controller.verify_frequency(target_hz,measured_hz)
            print("\nAssistant:",assistant.verification_message(current_hz,target_hz,measured_hz,verified)); break
    finally:
        controller.close()

    print("\nAssistant: What would you like the Arduino to do now? Say 'done' if nothing else.")
    request=input("You: ").strip()
    if request.lower() in {"done","no","nothing","quit","exit"}: return
    print("\nAssistant: I’ll modify the working sketch, compile it, and upload it.")
    ArduinoCodeAgent(config).generate_compile_upload(request)
    print("\nAssistant: The new sketch compiled and uploaded successfully.")

if __name__=="__main__": main()
