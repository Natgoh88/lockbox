#!/usr/bin/env bash
# Ask the Lockbox auditor deployed on AgentCore Runtime, and print only its answer (agentcore invoke streams events).
#   ./agentcore-auditor/ask.sh                      the DEVNET.md question
#   ./agentcore-auditor/ask.sh "your question"
cd "$(dirname "$0")/LockboxAuditor" || exit 1
Q="${1:-Explain what the referee did on the Lockbox account rnC9yaPArTeyTijnNbhgGajUTd2WHr4CzG from DEVNET.md, and whether every decision matched the rulebook.}"
printf '\033[1mLockbox auditor\033[0m  on Amazon Bedrock AgentCore Runtime (us-west-2), reading the public XRPL Devnet ledger\n\n> %s\n\n' "$Q"
PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1]}))' "$Q")
agentcore invoke --runtime agent --payload "$PAYLOAD" 2>&1 | python3 -u -c '
import json, sys
for line in sys.stdin:
    if not line.startswith("data: "):
        continue
    try:
        ev = json.loads(line[6:])["event"]
    except Exception:
        continue
    text = ev.get("contentBlockDelta", {}).get("delta", {}).get("text")
    if text:
        sys.stdout.write(text); sys.stdout.flush()
print()'
