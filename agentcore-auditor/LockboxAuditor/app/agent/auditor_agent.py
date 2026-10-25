"""The auditor's model and tools, without the AgentCore server: used by main.py and by the dashboard's
"Ask the auditor" endpoint. The model only explains; it never signs, sends or decides anything."""
from __future__ import annotations

import os
from pathlib import Path

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
  say so. For how many decisions of each kind there were, use verify_lockbox's summary counts; do not count rows
  yourself.
- You are read-only: you cannot sign, send or change anything, and you never ask for keys or seeds.
- Write for someone who is not an XRPL expert. Be clear and short. Plain text only: no markdown, no bold, no
  headings, no code formatting. Do not use em dashes."""


def make_tools(src=auditor.DEVNET) -> list:
    """The three read-only tools, reading from src: the public Devnet ledger, or a snapshot of a simulated one."""

    @tool
    def get_account_history(address: str) -> dict:
        """Read a lockbox account from the ledger: its balance, keys and signers, and every payment it made with
        who signed it.

        Args:
            address: the lockbox's XRPL classic address, starting with r
        """
        return auditor.get_account_history(address, src=src)

    @tool
    def verify_lockbox(address: str) -> dict:
        """Check a lockbox's referee. Confirms the published rulebook fingerprint matches the loan broker's Data
        field on the ledger, then replays every referee decision and reports whether it was signed or refused, the
        deciding rule, what that rule means, and whether the replay matches.

        Args:
            address: the lockbox's XRPL classic address, starting with r
        """
        return auditor.verify_lockbox(address, src=src)

    @tool
    def get_rulebook() -> dict:
        """Return the published Lockbox rulebook (lending-covenant v1) and its sha256 fingerprints."""
        return auditor.get_rulebook(src=src)

    return [get_account_history, verify_lockbox, get_rulebook]


ANTHROPIC_MODEL = "claude-opus-5-5"
OLLAMA_HOST = "https://ollama.com"
OLLAMA_MODEL = "gpt-oss:120b"  # a cloud model with tool calling; names come from https://ollama.com/api/tags


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
    """ollama, anthropic, bedrock or offline. auto (the default) takes the first one that has credentials."""
    m = os.environ.get("LOCKBOX_AUDITOR_MODE", "auto")
    if m in ("ollama", "anthropic", "bedrock", "offline"):
        return m
    if os.environ.get("OLLAMA_API_KEY"):
        return "ollama"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    try:
        import boto3
        return "bedrock" if boto3.Session().get_credentials() is not None else "offline"
    except Exception:
        return "offline"


def build_model(m: str):
    if m == "ollama":
        from strands.models.ollama import OllamaModel
        return OllamaModel(os.environ.get("LOCKBOX_OLLAMA_HOST", OLLAMA_HOST),
                           ollama_client_args={"headers": {"Authorization": f"Bearer {os.environ['OLLAMA_API_KEY']}"}},
                           model_id=os.environ.get("LOCKBOX_MODEL_ID", OLLAMA_MODEL))
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


def make_agent(m: str, src=auditor.DEVNET) -> Agent:
    """A fresh agent whose tools read only from src. Nothing it can call signs, sends or changes anything."""
    return Agent(model=build_model(m), system_prompt=SYSTEM_PROMPT, tools=make_tools(src), callback_handler=None)


def agent(m: str) -> Agent:
    if m not in _agents:
        _agents[m] = make_agent(m)
    return _agents[m]


def ask(prompt: str) -> dict:
    m = mode()
    if m == "offline":
        return {"result": answer.answer(prompt), "mode": m}
    a = agent(m)
    a.messages.clear()  # every question stands alone: no memory between callers
    return {"result": answer.clean(str(a(prompt))), "mode": m, "model": getattr(a.model, "config", {}).get("model_id")}


load_env()
