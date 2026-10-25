"""Copy the Lockbox auditor into the AgentCore app, then refuse to continue if anything secret came along.

  python agentcore-auditor/sync.py

Copies into LockboxAuditor/app/agent/:
  auditor.py, ledger.py, answer.py     the read-only tools (from agent/)
  auditor_agent.py                     the system prompt and the tool factory (agent/model.py)
  lockbox/                             only the five modules the replay needs, no module that can sign or submit
  data/                                the published decision logs and rulebooks/lending-covenant-v1.json
Then runs agent/build.py's check over app/agent: no wallet file, seed, key or signing code may be in it.
Run it again whenever agent/ or the rulebook changes, before agentcore deploy.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
APP = HERE / "LockboxAuditor" / "app" / "agent"
AGENT = REPO / "agent"

sys.path.insert(0, str(AGENT))
import build  # noqa: E402  agent/build.py: the same module list and secret check as the local build

COPIES = {"auditor.py": "auditor.py", "ledger.py": "ledger.py", "answer.py": "answer.py", "model.py": "auditor_agent.py"}
SKIP = {".venv", "__pycache__", "uv.lock"}


def sync() -> None:
    for src, dst in COPIES.items():
        shutil.copy2(AGENT / src, APP / dst)
    lb = APP / "lockbox"
    if lb.exists():
        shutil.rmtree(lb)
    lb.mkdir()
    for name in build.LOCKBOX_MODULES:
        shutil.copy2(REPO / "lockbox" / name, lb / name)
    data = APP / "data"
    if data.exists():
        shutil.rmtree(data)
    shutil.copytree(AGENT / "data", data)
    (data / "rulebooks").mkdir(exist_ok=True)
    shutil.copy2(REPO / "rulebooks" / "lending-covenant-v1.json", data / "rulebooks" / "lending-covenant-v1.json")


def check() -> list[str]:
    """build.check over what AgentCore will package: app/agent without its local .venv."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "agent"
        shutil.copytree(APP, out, ignore=lambda d, names: [n for n in names if n in SKIP])
        return build.check(out)


if __name__ == "__main__":
    sync()
    problems = check()
    for p in problems:
        print("SYNC FAILED:", p)
    if problems:
        sys.exit(1)
    print(f"OK: {APP.relative_to(REPO)} has the auditor, no wallet file, no seed, no key, no signing code")
