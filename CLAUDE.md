# Lockbox: instructions for Claude Code

Hackathon: AI & Fintech XRPL Hackathon, New York, Lending & Borrowing track.
Hard deadline: **Sunday 25 Oct, 1:00 PM New York**. Late submissions are not judged. Freeze code at 9:00 AM Sunday.
Only work done during the event counts, so commit small and often with clear messages.

Writing style for anything user-facing (README, dashboard copy, commit messages): plain English, no em dashes, sound like a person, not an AI.

## What Lockbox is

The tri-party lending agreement, enforced by the XRP Ledger itself.

1. XLS-66 loans have no collateral and leave enforcement to the lender, off the ledger.
2. Lockbox turns the borrower's revenue account into a multi-signed account (borrower, lender, referee) with the master key disabled. The referee signs only what a public rulebook allows: repayment comes first, the loan can only buy what it was for, and the account tightens itself when revenue drops.
3. Loans are sized from the account's real payment history (deterministic underwriting, every number explained).

Main persona for the demo: an **SME exporter** paid by foreign buyers (customers = buyers paying invoices, approved payees = suppliers). Keep the bakery only as the simple explanation.

## Where the code is today (all passing: `pytest`, 63 tests, no network)

| File | Status |
| --- | --- |
| `lockbox/inflows.py` | account_tx parsing, delivered_amount, XRP only. Done |
| `lockbox/underwrite.py` | mean minus one std, coverage 2.0, annuity sizing, score and tiers. Done |
| `lockbox/covenant.py` | Rulebook v1: LoanSet terms check, LoanPay, allowlist withdrawals, recovery after default. Needs the upgrades below |
| `lockbox/roles.py` | BorrowerAgent, PolicyService (the referee), Servicer (collect, impair, default, recover, late pay). Needs upgrades |
| `lockbox/sim.py` | In-memory ledger: signer quorum and weights, master key, vault, broker, loan, default, cover |
| `lockbox/chain.py` | Real XRPL backend via xrpl-py. **Never run against a network.** Signing paths checked offline |
| `lockbox/scenario.py`, `simsetup.py`, `devnet.py` | One story on sim or Devnet |
| `api/app.py`, `dashboard/index.html` | Dashboard, sim mode and Devnet autopilot |
| `scripts/01..06`, `story.py`, `offline_demo.py` | Devnet setup, lender, revenue, borrow, collect, mainnet scorer, terminal story, offline dashboard |

Run first: `pytest`, then `python scripts/story.py`, then `python scripts/offline_demo.py` (http://localhost:8403).

## Network facts (check, do not assume)

- Lockbox uses the **native XRP Ledger** (addresses start with `r`). NOT the XRPL EVM sidechain (0x addresses, MetaMask). Never use the EVM faucet.
- The Lending Protocol (XLS-66) and Single Asset Vault (XLS-65) are amendments in voting. They have been on **Devnet** (`https://s.devnet.rippletest.net:51234`). Ask the organizers which network has them for this event. `scripts/01_setup_devnet.py` checks the amendments first; set `XRPL_RPC` to try another network.
- Test RLUSD faucet is **Testnet only**, 10 RLUSD per day. Testnet RLUSD issuer: `rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV`. Accounts need 1 XRP and a trust line first. Only useful if lending is live on Testnet.
- Loans are in **XRP** for now. RLUSD (or a clearly labelled test dollar token) is a stretch goal after the Devnet gate.
- xrpl-py 5.2: LoanSet first party (Account = lender) must be single-signed; the counterparty (lockbox) may be multi-signed via `sign_loan_set_by_counterparty(wallet, tx, multisign=True)` then `combine_loanset_counterparty_signers`. Already implemented in `chain.py`.

## Build milestones (in order). Each one: code, tests, then commit.

Keep every new rule as a pure function with unit tests before wiring it to the sim. Never weaken an existing test to make it pass; if a test encodes old behaviour that the spec below changes, update it on purpose and say so in the commit message.

### M1. Key setup: referee required on every payment (about 45 min)
- Signer list becomes weighted: referee weight 2, borrower 1, lender 1, quorum 3. So every payment needs the referee plus one other. Borrower + lender alone can no longer move money (protects depositors from a broker colluding with a fake borrower).
- Update `protocol.lockdown_signer_list`, `sim.py` quorum checks, `Servicer.verify_lockbox` (check weights), and LoanSet counterparty signers (borrower + referee).
- Tests: borrower+lender refused, referee+borrower allowed, referee+lender allowed, referee alone refused.

### M2. Rulebook upgrades in `covenant.py` (about 2 hours)
1. **Buckets rebuilt from ledger history** (new `lockbox/buckets.py`): pure function over account_tx plus loan facts that returns reserve, fee buffer, optional bond, next-installment reserve, unspent principal, earned free cash. Same input, same output, so anyone can recompute it.
2. **Approved payees**: lender-approved list `{address: {cap_drops, invoice_ref}}`. Payments from the unspent-principal bucket only to these, up to the cap, and the payment must carry the invoice reference in a Memo. Money coming back from an approved payee returns to unspent principal, never to free cash.
3. **Withdrawals to the borrower's own wallet**: only from earned free cash.
4. **Automatic freeze on ledger facts only**: no withdrawals if a payment is past due, or if measured revenue fell short of the installment for two intervals in a row. Do NOT freeze on the lender's impaired flag (the lender could use it to pressure a healthy borrower). Update the existing test that expects an impaired freeze.
5. **Holdback setting**: reserve N installments (default 1) and optionally a percentage of each incoming payment.
6. **Optional bond**: borrower's own XRP posted before the loan (Payment into the lockbox with memo `bond`). Unspendable until release. On default the bond is swept to the lender first.
7. **Approval hygiene**: the referee only signs a transaction with `LastLedgerSequence` no more than 10 ledgers ahead, with `Sequence` equal to the account's next sequence, and only one outstanding approval at a time. Sim needs a ledger index that advances with time.
8. **XRP fee reserve**: always keep enough XRP for fees, even when loans move to stablecoins.
9. **Release after full repayment**: when the loan is paid and nothing is owed, the referee co-signs `SetRegularKey` to the borrower's registered key; the borrower then removes the signer list with that key. XRPL refuses to remove an account's last way to sign, so the order matters. Mark as needs Devnet check.
10. **Signed acceptance**: the borrower signs the hash of the exact terms; the referee verifies the signature before co-signing LoanSet.
11. **Rulebook and agreement hashes**: sha256 of the rulebook config goes in the LoanBroker `Data` field at broker creation; sha256 of terms plus approved payees goes in the LoanSet `Data` field. Check the field size on Devnet.
- Tests for every rule, including the attacks: payee refund laundering, two approvals stacked, stale approval submitted late, withdraw principal to own wallet, lender impair does not freeze, release refused while a loan is open.

### M3. Underwriting upgrades in `underwrite.py` (about 1.5 hours)
- **Sender cleaning**: ignore senders that are brand new (first seen fewer than N intervals ago) or were funded by the borrower, lockbox or payout wallet. Cap any single sender at 40% of counted revenue.
- **Locked vs unlocked quote**: same borrower priced twice. Unlocked = rate plus 6 points and half the limit. Label these as our assumptions in the UI.
- **Stress test**: show coverage if revenue drops 30%.
- **Graduation**: limit multiplier grows with loans this lockbox repaid on the ledger.

### M4. Dashboard (about 1.5 hours)
- SME exporter persona labels (buyers, suppliers).
- Bucket bar (reserve, bond, next installment, unspent principal, earned free cash).
- Locked vs unlocked quote side by side, stress test line.
- Depositor view: vault assets, yield earned, who loses first (bond, then broker cover, then depositors).
- Demo buttons: pay approved supplier (allowed), send principal to own wallet (refused), withdraw free cash, advance with revenue on, revenue stops, release after repayment.

### M5. Devnet (teammate, in parallel from the start)
- `python scripts/01_setup_devnet.py`, then `02_lend_setup.py`, then `03_revenue.py --minutes 8` in its own terminal, then `04_borrow.py`.
- **Go or no-go about 2 hours after starting**: vault, broker, cover, LoanSet and one LoanPay validated on Devnet. If not, the demo runs on the simulated ledger and we say so.
- First-run checks are listed in README "First Devnet run". Most likely fixes: fee for the multi-signed LoanSet, role order, early LoanPay, Number field parsing.
- After M1 to M2 land, rerun on Devnet with the weighted signer list.

### M6. Stretch, only if everything above works
- Stablecoin version (RLUSD on Testnet if lending is there, else a labelled test dollar token on Devnet).
- Three referees with weighted keys (say it in the pitch even if not built).
- XRPL Credentials for KYB-gated vault.

## Scope cuts if behind (in this order)
Graduation, stress test, sender cap, bond, release path, RLUSD.
Never cut: referee required (M1), approved payees, automatic freeze, the withdraw-everything refusal demo.

## Things to be careful of
- `.wallets.json` holds seeds and is gitignored. Never commit it or paste seeds anywhere.
- Never present simulated numbers as real. Every screen says which ledger it is on.
- Interest on a minutes-long loan is a few drops. Demo lender income is the service fee. Say so.
- Disclose AI assistance in the README.
- Keep `chain.py` the only file that talks to the network.
- Run `pytest` before every commit.

## Team split (respect file ownership)
- Nathaniel: covenant.py, buckets.py, roles.py, sim.py, protocol.py and their tests. Milestones M1, M2.
- Teammate: chain.py, scripts/, underwrite.py, inflows.py, api/, dashboard/, README.md, tests/test_attacks.py, and the only .wallets.json. Devnet (M5), M3, M4, red-team attack tests.
- Never edit the other person's files without asking. Pull before starting, commit and push small changes often.
- The dashboard depends on scenario.state(): add fields, never rename or remove existing ones.
