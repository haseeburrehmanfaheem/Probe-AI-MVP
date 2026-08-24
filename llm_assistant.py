import os
from enum import Enum
from typing import Optional
from openai import OpenAI
from pydantic import BaseModel
class Action(str,Enum): change="change"; no_change="no_change"; clarify="clarify"
class UserIntent(BaseModel): action:Action; target_hz:Optional[int]; message:str
class FrequencyAssistant:
    def __init__(self,allowed_rates): self.allowed_rates=sorted(allowed_rates); self.client=OpenAI(api_key=os.environ.get("OPENAI_API_KEY")); self.model=os.environ.get("OPENAI_MODEL","gpt-5.4-mini-2026-03-17")
    def opening_message(self,measured_hz):
        r=self.client.responses.create(model=self.model,input=f"Current sensor frequency is {measured_hz:.2f} Hz. Supported rates: {self.allowed_rates}. Tell the user the frequency and ask whether they want to change it. Be concise."); return r.output_text.strip()
    def interpret_reply(self,user_reply,current_hz):
        r=self.client.responses.parse(model=self.model,input=f"Current: {current_hz:.2f} Hz\nSupported: {self.allowed_rates}\nUser: {user_reply}\nInterpret as change, no_change, or clarify. If change, target_hz must be supported.",text_format=UserIntent); intent=r.output_parsed
        if intent.action==Action.change and intent.target_hz not in self.allowed_rates: return UserIntent(action=Action.clarify,target_hz=None,message=f"Choose one of {self.allowed_rates} Hz.")
        return intent
    def verification_message(self,old_hz,target_hz,measured_hz,verified):
        return f"Done — changed to {target_hz} Hz and verified at {measured_hz:.2f} Hz." if verified else f"The command was sent, but verification measured {measured_hz:.2f} Hz instead of about {target_hz} Hz."
