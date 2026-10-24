"""The one interface between the logic and a ledger. Two implementations: sim.SimChain and chain.XrplChain.

Keys are opaque. In the sim a key is just an address string. On the real ledger it is an xrpl Wallet.
`addr_of(key)` gives the address either way.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .covenant import LoanView
from .health import BrokerView, VaultView


def addr_of(key) -> str:
    return key if isinstance(key, str) else key.address


@dataclass
class Result:
    ok: bool
    code: str
    tx: dict
    meta: dict = field(default_factory=dict)
    hash: str = ""

    @property
    def created(self) -> dict:
        from .protocol import created_ids
        return created_ids(self.meta)


class Backend(Protocol):
    def now(self) -> int: ...
    def balance(self, addr: str) -> int: ...
    def reserve(self, addr: str) -> int: ...
    def account_tx(self, addr: str) -> list[dict]: ...
    def lockbox_state(self, addr: str) -> dict: ...
    def prepare(self, tx: dict, signers_count: int = 0) -> dict: ...
    def submit(self, tx: dict, signers: list, counterparty_signers: list | None = None) -> Result: ...
    def loan_view(self, loan_id: str) -> LoanView | None: ...
    def broker_view(self, broker_id: str) -> BrokerView: ...
    def vault_view(self, vault_id: str) -> VaultView: ...
