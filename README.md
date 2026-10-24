# Lockbox

Lend against the cash a business is about to earn, and let the account itself enforce the repayment.

Built for the AI & Fintech XRPL Hackathon, Lending & Borrowing track. Only work done during the event counts, so build on top of this on the day and commit as you go.

## The idea in three lines

1. XLS-66 loans are uncollateralized and the protocol deliberately leaves underwriting and enforcement off-chain. Nothing stops a borrower from moving the money away after the loan is funded.
2. Lockbox makes the borrower's revenue account a multisig (borrower, lender, neutral referee) with the master key switched off. The referee is required on every payment, plus one of the other two. A rulebook the referee applies decides which transactions get its signature. The borrower can spend freely, but never the next installment.
3. The loan is sized from the account's own ledger history (what really arrived), and the lender collects, impairs, defaults and recovers with the referee, with no borrower signature needed.

## Honest status

| Part | Status |
| --- | --- |
| Underwriting math, inflow parsing, covenant rules, health math, tx builders | unit tested (63 tests) |
| Full story on the in-memory ledger: lock, quote, LoanSet, autopay, withdraw refusals, late payment, impair, default, first-loss cover, recovery | passes (tests/test_end_to_end.py, scripts/story.py) |
| Real backend signing: LoanSet with a single-signed first party and a multi-signed counterparty, multisigned payments | checked offline by capturing what would be submitted (tests/test_chain_offline.py) |
| Everything that talks to Devnet (chain.py, scripts 01 to 06) | written, NOT run. The sandbox it was built in cannot reach XRPL. Expect small fixes on the first run. |
| Closed-ended vaults (LendingProtocolV1_1) | not implemented. Open-ended vault only, which is what the xrpl.org tutorials use. |

Things the sim does NOT reproduce: real amortization rounding (the sim uses a plain annuity, the real servicer reads `PeriodicPayment` from the ledger), real fees, and the exact late-payment rules. The late-payment path (`tfLoanLatePayment`) is the most likely to need a tweak on Devnet.

On a 12 minute Devnet demo, interest is a few drops. Lender income in the demo is the per-payment `LoanServiceFee`. Say that out loud. Never present demo yields as market data.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                              # 63 tests, no network

python scripts/story.py             # the whole story in the terminal
python scripts/offline_demo.py      # dashboard on the simulated ledger, http://localhost:8403
```

Real Devnet (five terminals worth of steps, about 15 minutes of wall time because loans run in real seconds):

```bash
python scripts/01_setup_devnet.py   # checks amendments, funds 7 wallets from the faucet
python scripts/02_lend_setup.py     # vault, loan broker, liquidity, first-loss cover
python scripts/03_revenue.py --minutes 8     # customers pay the lockbox: the history the underwriter reads
python scripts/04_borrow.py         # lock the account, underwrite, LoanSet
LOCKBOX_MODE=devnet uvicorn api.app:app --port 8403    # dashboard + autopilot servicer
# or headless:
python scripts/05_collect.py --revenue           # good borrower
python scripts/05_collect.py --withdraw-free     # then rerun without --revenue to force a default
python scripts/06_score_address.py rYourAddress  # read-only underwriting of a real mainnet address
```

## How it works

```
customers ──pay──> LOCKBOX account (quorum 3, master key off)
                      signers: policy referee (weight 2) · borrower agent (1) · lender servicer (1)
                      referee plus one other moves money, the referee only signs what covenant.py allows

lender (vault owner + broker owner)
   VaultCreate, VaultDeposit, LoanBrokerSet, LoanBrokerCoverDeposit       first-loss cover
   LoanSet   Account = lender (one signature), Counterparty = lockbox (two of its signers inside CounterpartySignature)
   LoanPay   Account = lockbox, signed by lender + referee (autopay) or borrower + referee
   LoanManage impair / default, signed by the lender
```

Why the referee is required: the borrower alone cannot spend the lender's money, the lender alone cannot touch the borrower's cash, the referee alone can do nothing, and the lender plus referee can collect and recover even if the borrower disappears. Borrower plus lender cannot move money without the referee either, so a broker working with a fake borrower cannot drain the account and leave the depositors with the loss.

Why the lockbox is locked BEFORE the loan: `LoanSet` needs a counterparty signature, and xrpl-py allows that signature to be multi-signed. The first party must be single-signed, so the lender is the first party and the lockbox is the counterparty. The lender reads the lockbox's on-ledger state (master key off, no regular key, exactly the three signers with referee weight 2, borrower 1, lender 1, quorum 3) and refuses to originate if any of it is wrong.

### The covenant (lockbox/covenant.py)

| Transaction | Allowed when |
| --- | --- |
| LoanSet | the borrower accepted exactly these terms, no active loan, same broker, principal, schedule, fees not higher |
| LoanPay | right loan, amount between the installment and installment + a small allowance, no overpay or full-payment flag, late-payment flag only after the due date |
| Payment to the borrower's allowlist | loan current (not impaired, not past due), amount within free cash |
| Payment to the lender | only after default, only up to what was owed |
| anything else | refused (SignerListSet, AccountSet, SetRegularKey, DelegateSet, AccountDelete, trust lines, offers, paths, SendMax, partial payments) |

Free cash = balance - ledger reserve - next installment - fee buffer.

### Underwriting (lockbox/underwrite.py)

Deterministic, no model. Revenue is bucketed by payment interval from `account_tx`, measured by `meta.delivered_amount`, XRP only. Conservative inflow = mean - one standard deviation. Installment is capped at conservative inflow divided by a coverage target (2.0). Principal is the annuity present value of that installment. The score (history, volatility, customer spread, recency) only picks the tier and the rate premium or declines. It never raises the limit. Every number comes with a reason line.

## Known limits (say them before a judge does)

- The covenant protects the next installment and freezes withdrawals once the loan is late. It does not control use of proceeds: the borrower can withdraw the loan principal itself as free cash. Raise `reserve_installments` or add a revenue holdback to tighten it.
- Default recovery sweeps what is in the lockbox at the time, up to what was owed. If the borrower already took the free cash, the first-loss cover is what absorbs the loss.
- Fixed-term loans, XRP only, one active loan per lockbox.
- The referee in this repo runs in the same process as the other roles. In a real deployment it is a separate server that receives the unsigned transaction and returns its signature, and the borrower's acceptance of terms carries the borrower's signature.
- Lending Protocol is a draft standard on Devnet. Field names and flags can still move.
- Revenue history on Devnet is synthetic (our own customer wallets). Script 06 shows the underwriter on real mainnet history, read-only.

## First Devnet run: things to check

1. `01_setup_devnet.py` prints the amendment status. If LendingProtocol is off on the URL you use, find the current Devnet URL on xrpl.org.
2. `LoanSet` fee with a multi-signed counterparty. `prepare` asks xrpl-py for a two-signer fee. If you get `telINSUF_FEE_P` or `tecINSUFFICIENT_FEE`, raise it in `XrplChain.prepare`.
3. If `LoanSet` returns `tecNO_PERMISSION`, the roles may be the other way round for your amendment version. Swap Account and Counterparty in `protocol.loan_set` and the signing order in `chain.XrplChain.submit`.
4. Early `LoanPay` (before the due date). The servicer pays at 25% of the interval before the due date. If the ledger rejects early payments, set `Params.pay_lead_fraction = 0`.
5. `LoanPay` with `tfLoanLatePayment`: the amount must match the ledger's exact late amount. With no late fee or late interest set on the loan it should equal the installment.
6. Number fields (`PeriodicPayment`, `TotalValueOutstanding`) are parsed by `chain._num`. Check them against a real ledger entry.
7. Found on the first run: with `LendingProtocolV1_1` enabled, `LoanBrokerSet` returns `tecNO_PERMISSION` unless the vault is closed-ended. `02_lend_setup.py` now makes one: deposits until the subscription date (default 5 min), loans only after it, and every loan must end 60 s before the redemption date (default 75 min later). Run `04_borrow.py` after the subscription date.

## Layout

```
lockbox/units.py       drops, ripple time, rate units
lockbox/inflows.py     account_tx -> clean list of incoming XRP payments
lockbox/underwrite.py  cash-flow underwriting, deterministic
lockbox/covenant.py    the referee's rulebook
lockbox/health.py      cover ratio, utilization, loan status
lockbox/protocol.py    transaction builders, created-object ids
lockbox/backend.py     the interface between logic and a ledger
lockbox/sim.py         in-memory ledger: signer quorums, vault, broker, loan, default, cover
lockbox/chain.py       the real ledger over xrpl-py (the only file that touches the network)
lockbox/roles.py       BorrowerAgent, PolicyService, Servicer
lockbox/scenario.py    one story, same code on sim and Devnet
api/app.py             dashboard + control API (sim or devnet autopilot)
dashboard/index.html   live view
scripts/               setup, lender, revenue, borrow, collect, mainnet scorer, story, offline demo
tests/                 unit, sim end-to-end, offline signing checks
```

## What to build on the day (in order)

1. Run `pytest` and `scripts/story.py` (5 minutes). Then Devnet steps 01 and 02. Gate: vault, broker, cover and deposit work. If they do not by hour 2, demo on the sim and say so.
2. Get one real `LoanSet` through with the multi-signed counterparty, then one `LoanPay` from the lockbox. Fix the first-run items above.
3. Run the good path and the default path on Devnet, record both.
4. Run `06_score_address.py` on a real mainnet address and show it next to the Devnet run.
5. Polish the dashboard, record the demo, write the README story, commit often.

## Safety notes

- `.wallets.json` holds Devnet seeds. It is gitignored. Do not use these wallets anywhere real.
- The lockbox master key is disabled on purpose and cannot be re-enabled by one party. Never run `lock()` on an account you care about.
- Read-only scoring (script 06) signs nothing. Use it on addresses you own or public business addresses.
