"""Lockbox auditor: a read-only agent that explains what a lockbox's referee did, from the public ledger.

  python main.py                      serve on http://localhost:8080 (POST /invocations, GET /ping), like AgentCore Runtime
  python main.py --ask "question"     answer one question in the terminal

The answer is written by an AI model that calls the three read-only tools:
  ollama     an open model on Ollama Cloud, when OLLAMA_API_KEY is set (put it in agent/.env)
  anthropic  Claude through the Anthropic API, when ANTHROPIC_API_KEY is set (put it in agent/.env)
  bedrock    Claude through Amazon Bedrock, when AWS credentials are available (AgentCore Runtime)
  offline    no model: answer.py writes the same facts from a fixed template
LOCKBOX_AUDITOR_MODE picks one; auto (the default) takes the first that is available, in that order.
Either way nothing is signed or sent: every tool only reads the ledger.
"""
from __future__ import annotations

import sys

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from model import SYSTEM_PROMPT, agent, ask, build_model, load_env, make_agent, make_tools, mode  # noqa: F401
import auditor

get_account_history, verify_lockbox, get_rulebook = make_tools(auditor.DEVNET)

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
