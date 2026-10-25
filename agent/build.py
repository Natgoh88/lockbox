"""Build the auditor's deployment folder and refuse to ship anything secret.

  python build.py            build dist/ and check it
  python build.py --check D  only check folder D

dist/ holds the agent code, the five lockbox modules the replay needs (no module that can sign or submit),
the published rulebook and the referee's published decision logs. The check fails the build if it finds a
wallet file, a seed, a private key, an AWS key, signing code, or any seed from the repo's .wallets.json.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DIST = HERE / "dist"

AGENT_FILES = ["main.py", "auditor.py", "answer.py", "ledger.py", "pyproject.toml"]
LOCKBOX_MODULES = ["__init__.py", "verify.py", "covenant.py", "buckets.py", "inflows.py", "units.py"]

FORBIDDEN_NAMES = re.compile(r"(^|/)(\.wallets\.json|\.env(\..*)?|.*\.pem|.*\.key|credentials)$")
FORBIDDEN_MODULES = {"chain.py", "roles.py", "scenario.py", "devnet.py", "sim.py", "simsetup.py", "protocol.py"}
SECRET_PATTERNS = {
    "XRPL seed": re.compile(r"(?<![0-9A-Za-z])s[1-9A-HJ-NP-Za-km-z]{28,30}(?![0-9A-Za-z])"),
    "AWS access key": re.compile(r"(?<![0-9A-Z])(AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"),
    "AWS secret key setting": re.compile(r"aws_secret_access_key", re.I),
    "private key": re.compile(r"PRIVATE KEY-----|\"private_key\"\s*:"),
    "seed field": re.compile(r"\"(seed|secret|master_seed)\"\s*:"),
}
SIGNING_IMPORTS = re.compile(r"from xrpl\.(wallet|transaction)|import xrpl\.(wallet|transaction)|\bsubmit_and_wait\b|\bautofill\b")


def _known_seeds() -> list[str]:
    p = REPO / ".wallets.json"
    if not p.is_file():
        return []
    try:
        return [v for v in json.loads(p.read_text()).values() if isinstance(v, str) and v]
    except (ValueError, AttributeError):
        return []


def check(folder: Path) -> list[str]:
    """Problems found in folder. Never prints a secret, only where it is."""
    problems, seeds = [], _known_seeds()
    for f in sorted(folder.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(folder).as_posix()
        if FORBIDDEN_NAMES.search(rel):
            problems.append(f"{rel}: wallet or secret file must not be shipped")
        if f.name in FORBIDDEN_MODULES:
            problems.append(f"{rel}: signing or submitting code must not be shipped")
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for name, pat in SECRET_PATTERNS.items():
            if pat.search(text):
                problems.append(f"{rel}: looks like it contains a {name}")
        if any(s in text for s in seeds):
            problems.append(f"{rel}: contains a seed from .wallets.json")
        if f.suffix == ".py" and not rel.startswith("lockbox/") and SIGNING_IMPORTS.search(text):
            problems.append(f"{rel}: imports signing or submitting code")
    return problems


def build() -> Path:
    if DIST.exists():
        shutil.rmtree(DIST)
    (DIST / "lockbox").mkdir(parents=True)
    for name in AGENT_FILES:
        shutil.copy2(HERE / name, DIST / name)
    for name in LOCKBOX_MODULES:
        shutil.copy2(REPO / "lockbox" / name, DIST / "lockbox" / name)
    shutil.copytree(HERE / "data", DIST / "data")
    (DIST / "data" / "rulebooks").mkdir(exist_ok=True)
    shutil.copy2(REPO / "rulebooks" / "lending-covenant-v1.json", DIST / "data" / "rulebooks" / "lending-covenant-v1.json")
    for p in DIST.rglob("__pycache__"):
        shutil.rmtree(p)
    return DIST


def main(argv: list[str]) -> int:
    folder = Path(argv[argv.index("--check") + 1]) if "--check" in argv else build()
    problems = check(folder)
    for p in problems:
        print("BUILD FAILED:", p)
    if problems:
        return 1
    files = [f for f in folder.rglob("*") if f.is_file()]
    print(f"OK: {folder} has {len(files)} files, no wallet file, no seed, no key, no signing code")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
