"""
llm.py - the only way anything reaches the model, and what it is told.

llm_json() refuses to send a payload in which the PII vault still finds customer data, and keeps an
exact copy of every payload in the run folder (llm_NN.json) so a reviewer can see what the model saw.
"""
import json

from dotenv import load_dotenv

from cua import settings
from cua.errors import Stop

load_dotenv(settings.ROOT / ".env")                 # OPENAI_API_KEY
client = None


def llm_json(system, payload, vault, run=None):
    """Masked payload -> the model's JSON reply."""
    global client
    leaks = vault.leaks(payload)
    if leaks:
        kinds = sorted({x.split(":")[0] for x in leaks})
        raise Stop(f"blocked a GPT-4o call: {len(leaks)} unmasked value(s) ({', '.join(kinds)})",
                   code="pii_leak_blocked")
    if run:
        run.save_llm(payload)
    from openai import OpenAI
    client = client or OpenAI()
    r = client.chat.completions.create(
        model=settings.MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    return json.loads(r.choices[0].message.content)


ROUTER_SYSTEM = """You map a request for a banking desktop application to ONE permitted intent.
Personal data in the request is masked as tokens like <TEXT_1>, <EMAIL_1>, <PHONE_1>, <ACCT_1>, <AMOUNT_1>;
a customer name usually appears as a <TEXT_n> token.
Permitted intents:
{intents}
Rules:
- Any other change to data (edit customer, deposit, withdraw, transfer, close or delete an account...) -> "not_allowed".
- A request that fits no intent -> "unknown".
- Details, info or a question about one customer or account (balance, status, contact details, transactions,
  owner...) -> account_details, also when it is given by a name that may match several accounts (the
  user then picks one). find_account only when the request asks to search for, find or list the matches.
- Slot values: copy tokens exactly as written; account_type may be the word savings or checking.
  Leave out slots the request does not give. Never invent values.
Reply with JSON only: {{"intent": "...", "slots": {{"<slot>": "<token or word>"}}, "reason": "<short>"}}"""


def router_system(profile):
    lines = [f"- {i.name} ({i.mode}): {i.about}; slots: {', '.join(s.name for s in i.slots) or 'none'}"
             for i in profile.intents.values()]
    return ROUTER_SYSTEM.format(intents="\n".join(lines))


STEP_SYSTEM = """You operate a Windows banking desktop application. You cannot see it: each turn you
get the current window as JSON produced by OCR. Complete the task one event per turn.
Customer data is masked: names, emails, phones, account numbers, amounts and other personal text
appear as tokens like <NAME_1>, <ACCT_2>, <AMOUNT_3>, <TEXT_4>. Work with the tokens as they are;
the program puts the real values back locally.

Events:
 {"type":"click","target":"<element id>"}
 {"type":"double_click","target":"<element id>"}
 {"type":"type","target":"<input id>","text":"..."}        (replaces the field content)
 {"type":"select","target":"<dropdown id>","value":"..."}
 {"type":"key","keys":"enter" | "tab" | "esc"}
 {"type":"wait","seconds":1}
 {"type":"ask","param":"<snake_case name>","question":"..."}   (a value you need was not given)
 {"type":"done","verify":{...},"outputs":{...}}
 {"type":"fail","reason":"..."}

Rules:
- Use only element ids that exist in the current screen JSON (buttons, menu, inputs, table row ids).
- Only the actions listed under "allowed" are possible; anything else is refused.
- Your steps are saved and replayed later for other values. Reach an account by searching for the
  parameter first; never rely on the row that happens to be selected or on a window left open.
- Table rows list under "matches" which parameters they contain. If several rows match, pick any of
  them - the program asks the user which one.
- "map" says which window you are in, where the known moves lead and how reliable they have been;
  prefer reliable moves.
- When typing or selecting a value from the parameters, write its placeholder, e.g. "{name}" -
  never the token and never a literal value.
- Never guess data. If a field needs a value that is not in the parameters, use "ask".
- Values listed under "check" in a table row may be OCR mistakes.
- Finish on the screen that shows the result. In "done":
  verify = {"text_visible":"<ONE text line exactly as shown, tokens included>"} or
  {"table_contains":{"<column>":"<token>"}};
  outputs = for each name under "outputs_wanted", the token (or exact text) on screen that holds it;
  for a table output give the text of its first column header.
- If an error message appears, use "fail" with the message as reason.
Reply with JSON only: {"thought":"<short>","event":{...},"expect":"<what the screen should show next>"}"""
