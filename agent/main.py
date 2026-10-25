"""Lockbox auditor: a read-only agent that explains what a lockbox's referee did, from the public ledger.

  python main.py                      serve on http://localhost:8080 (POST /invocations, GET /ping), like AgentCore Runtime
  python main.py --ask "question"     answer one question in the terminal

With AWS credentials it is a Strands agent on Amazon Bedrock that calls the three tools and writes the answer.
Without them (LOCKBOX_AUDITOR_MODE=offline, or auto with no credentials) the same tools run and the answer is
written by answer.py. Either way nothing is signed or sent: every tool only reads the ledger.
"""
from __future__ import annotations

import os
import sys

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

import answer
import auditor

SYSTEM_PROMPT = """You are the Lockbox auditor. A lockbox is an XRPL account whose every outgoing payment needs a
referee's signature. The referee follows a public rulebook. You check what the referee did, using only your tools,
which read the public XRPL Devnet ledger and replay the referee's published decisions against the rulebook.

How to answer:
- Call verify_lockbox for the address. Use get_account_history for the ledger details and get_rulebook for the rules.
- For each decision say whether the referee signed or refused it, which rule decided it, and what that rule means.
- Say whether the rulebook fingerprint matches the one on the ledger and whether every decision replayed the same.
- A refused payment never reaches the ledger. Say so instead of looking for a transaction hash.
- If a signed payment was later rejected by the ledger (for example tecEXPIRED), say that clearly.
- Only state facts the tools returned. Never guess amounts, addresses or results.
- You are read-only. You cannot sign, send or change anything, and you never ask for keys or seeds.
- Write plain English for a non-expert, short and direct. Do not use em dashes."""


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


def mode() -> str:
    """bedrock or offline. auto (the default) uses Bedrock when AWS credentials are available."""
    m = os.environ.get("LOCKBOX_AUDITOR_MODE", "auto")
    if m in ("bedrock", "offline"):
        return m
    try:
        import boto3
        return "bedrock" if boto3.Session().get_credentials() is not None else "offline"
    except Exception:
        return "offline"


_agent = None


def agent() -> Agent:
    global _agent
    if _agent is None:
        model = None
        if os.environ.get("LOCKBOX_MODEL_ID"):
            from strands.models import BedrockModel
            model = BedrockModel(model_id=os.environ["LOCKBOX_MODEL_ID"], region_name=os.environ.get("AWS_REGION"))
        _agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=[get_account_history, verify_lockbox, get_rulebook],
                       callback_handler=None)
    return _agent


def ask(prompt: str) -> dict:
    m = mode()
    text = str(agent()(prompt)) if m == "bedrock" else answer.answer(prompt)
    return {"result": answer.clean(text), "mode": m}


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
