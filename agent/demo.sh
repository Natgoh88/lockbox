#!/usr/bin/env bash
# A clean 30-second take for the AWS pitch: one question, one answer, read-only, from the public Devnet ledger.
#   cd agent && ./demo.sh            (start your screen recording first: Cmd+Shift+5)
cd "$(dirname "$0")"
Q="${1:-Why was the payment refused on lockbox r4UuCzXSK6UReG5enELvQQEwkudqnBWNXe?}"
clear
printf '\033[1mLockbox auditor\033[0m  read-only, on the public XRPL Devnet ledger\n\n'
sleep 1.5
printf '\033[36m> \033[0m'
for ((i = 0; i < ${#Q}; i++)); do printf '%s' "${Q:i:1}"; sleep 0.03; done
printf '\n\n'
.venv/bin/python main.py --ask "$Q" | fold -s -w "${COLUMNS:-100}"
