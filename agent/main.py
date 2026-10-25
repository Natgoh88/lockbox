"""Lockbox auditor: a read-only agent that explains what a lockbox's referee did, from the public ledger.

  python main.py                      serve on http://localhost:8080 (POST /invocations, GET /ping), like AgentCore Runtime
  python main.py --ask "question"     answer one question in the terminal

The answer is written by an AI model that calls the three read-only tools:
  anthropic  Claude through the Anthropic API, when ANTHROPIC_API_KEY is set (put it in agent/.env)
  bedrock    Claude through Amazon Bedrock, when AWS credentials are available (AgentCore Runtime)
  offline    no model: answer.py writes the same facts from a fixed template
LOCKBOX_AUDITOR_MODE picks one; auto (the default) takes the first that is available, in that order.
Either way nothing is signed or sent: every tool only reads the ledger.
"""
from __future__ import annotations

import os
import sys

from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

import answer
import auditor

SYSTEM_PROMPT = """You are the Lockbox auditor. You help people understand what a lockbox's referee did and why.

Background: a lockbox is a business's XRPL account, used for a loan. Its master key is switched off and every
payment out of it needs the referee's signature plus the borrower's or the lender's. The referee follows a public
rulebook whose fingerprint is written on the ledger. It signs a payment only if the rules allow it, for example:
repayments come first, loan money can only pay suppliers the lender approved, and the owner can only take out money
the business earned or put in itself.

Your tools only read the public XRPL Devnet ledger and the referee's published decision log. verify_lockbox replays
every decision with the published rules and says whether the replay matches what the referee did.

When you answer:
- Start with verify_lockbox for the address; use get_account_history and get_rulebook when they help.
- Explain each decision in a sentence or two of plain English: what was attempted, whether the referee signed or
  refused it, the rule that decided it, and why that rule exists in this situation. Group repeated decisions (such
  as several repayments) instead of listing each one the same way.
- For a refusal, say what was asked for and what the rules allowed at that moment, using the numbers the tools give.
- A refused payment never reaches the ledger, so it has no transaction hash. A signed payment can still be rejected
  by the ledger itself (for example tecEXPIRED when it lands after its due date); explain that difference.
- Say whether the rulebook on the ledger matches the published one and whether every decision replayed the same.
- Use only facts the tools returned. Never invent amounts, addresses, hashes or results; if something is missing,
  say so.
- You are read-only: you cannot sign, send or change anything, and you never ask for keys or seeds.
- Write for someone who is not an XRPL expert. Be clear and short. Do not use em dashes."""


@tool
def get_account_history(address: str) -> dict:
    """Read a lockbox account from the public XRPL Devnet ledger: its balance, keys and signers, and every
    payment it made with who signed it.

    Args:
        address: the lockbox's XRPL classic address, starting with r
    """
    return auditor.get_account_history(address)


@tool
def verify_lockbox(address: str) -> dict:
    """Check a lockbox's referee. Confirms the published rulebook fingerprint matches the loan broker's Data field
    on the ledger, then replays every referee decision and reports whether it was signed or refused, the deciding
    rule, what that rule means, and whether the replay matches.

    Args:
        address: the lockbox's XRPL classic address, starting with r
    """
    return auditor.verify_lockbox(address)


@tool
def get_rulebook() -> dict:
    """Return the published Lockbox rulebook (lending-covenant v1) and its sha256 fingerprints."""
    return auditor.get_rulebook()


ANTHROPIC_MODEL = "claude-opus-5-5"


def load_env(path: Path = Path(__file__).resolve().parent / ".env") -> None:
    """Read KEY=VALUE lines from agent/.env into the environment. A variable already set wins. Values are never
    printed or logged."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def mode() -> str:
    """anthropic, bedrock or offline. auto (the default) takes the first one that has credentials."""
    m = os.environ.get("LOCKBOX_AUDITOR_MODE", "auto")
    if m in ("anthropic", "bedrock", "offline"):
        return m
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    try:
        import boto3
        return "bedrock" if boto3.Session().get_credentials() is not None else "offline"
    except Exception:
        return "offline"


def build_model(m: str):
    if m == "anthropic":
        from strands.models.anthropic import AnthropicModel
        # Claude Opus 5.5 thinks adaptively on its own (no thinking setting); effort is set explicitly
        return AnthropicModel(client_args={"api_key": os.environ["ANTHROPIC_API_KEY"]},
                              model_id=os.environ.get("LOCKBOX_MODEL_ID", ANTHROPIC_MODEL), max_tokens=16000,
                              params={"output_config": {"effort": os.environ.get("LOCKBOX_EFFORT", "medium")}})
    if os.environ.get("LOCKBOX_MODEL_ID"):
        from strands.models import BedrockModel
        return BedrockModel(model_id=os.environ["LOCKBOX_MODEL_ID"], region_name=os.environ.get("AWS_REGION"))
    return None  # Strands' default Bedrock model


_agents: dict[str, Agent] = {}


def agent(m: str) -> Agent:
    if m not in _agents:
        _agents[m] = Agent(model=build_model(m), system_prompt=SYSTEM_PROMPT,
                           tools=[get_account_history, verify_lockbox, get_rulebook], callback_handler=None)
    return _agents[m]


def ask(prompt: str) -> dict:
    m = mode()
    if m == "offline":
        return {"result": answer.answer(prompt), "mode": m}
    a = agent(m)
    a.messages.clear()  # every question stands alone: no memory between callers
    return {"result": answer.clean(str(a(prompt))), "mode": m, "model": getattr(a.model, "config", {}).get("model_id")}


load_env()
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    return ask((payload or {}).get("prompt", ""))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--ask":
        out = ask(" ".join(sys.argv[2:]))
        print(f"[{out['mode']}]\n{out['result']}")
    else:
        app.run()
