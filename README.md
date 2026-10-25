# Lockbox

**A referee that makes loan agreements enforceable on XRPL.**

XRPL's lending protocol leaves enforcement off the ledger. Lockbox adds a referee that must co-sign every payment from the borrower's account and follows the loan agreement as a public rulebook anyone can verify. Lending is the first rulebook.

Built for the AI & Fintech XRPL Hackathon, Lending & Borrowing track.

## How it works

1. **The referee is required on every payment.** The borrower's revenue account becomes a multi-signed account with the master key switched off: referee weight 2, borrower 1, lender 1, quorum 3. Every payment needs the referee plus the borrower or the lender. The referee alone can do nothing, and the borrower and lender together cannot move money without it.
2. **The referee follows a public rulebook.** The rules are plain checks in code: repayment comes first, loan money can only pay suppliers the lender approved (with the invoice number on the payment), the owner can only take out what the business earned or put in, and the account freezes on ledger facts when a payment is late or revenue falls short. The rulebook's fingerprint is written in the loan broker's Data field and the agreement hash in the loan's Data field, so anyone can check the referee is using the published rules.
3. **Anyone can verify it.** `python scripts/verify.py` checks the published rulebook file against the fingerprint on the ledger, then replays every decision the referee made from the account's ledger history. The dashboard has the same check behind "Verify the referee".
4. **Lending is the first rulebook.** The loan is an XLS-66 loan from a closed-ended XLS-65 vault, sized from the account's own payment history (deterministic underwriting, every number explained). The lender collects, impairs, defaults and recovers through the referee, with no borrower signature needed.

The demo persona is a small exporter paid by foreign buyers: buyers pay invoices into the lockbox, and the loan pays the exporter's approved suppliers.

## Status

| Part | Status |
| --- | --- |
| Rulebook, referee, buckets, underwriting, servicer, verify | unit tested, 249 tests, no network |
| The whole story on the simulated ledger: lock, quote, loan, supplier paid, refusals, late payment, impair, default, first-loss cover, recovery, release | passes (tests, `scripts/story.py`, `scripts/verify.py`, the dashboard) |
| XRPL Devnet: closed-ended vault, broker with the rulebook fingerprint, weighted lock, LoanSet, LoanPays, release after repayment | ran on Devnet with test XRP. Explorer links in DEVNET.md |

The dashboard runs on a simulated ledger and says so on screen; DEVNET.md has the same flow on the real XRP Ledger Devnet. On a loan that lasts minutes, interest is a few drops, so the lender's income in the demo is the per-payment service fee. A real loan runs for months. The quote "without a lockbox" is our assumption, not market data.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                              # 249 tests, no network

python scripts/story.py             # the whole story in the terminal
python scripts/offline_demo.py      # dashboard on the simulated ledger, http://localhost:8403
```

Real Devnet (five terminals worth of steps, about 15 minutes of wall time because loans run in real seconds):

```bash
python scripts/01_setup_devnet.py   # checks amendments, funds 7 wallets from the faucet
python scripts/02_lend_setup.py     # vault, loan broker, liquidity, first-loss cover
python scripts/03_revenue.py --minutes 8     # customers pay the lockbox: the history the underwriter reads
python scripts/04_borrow.py         # lock the account, underwrite, LoanSet. Run it while 03 is still paying
# start the servicer straight away: the first installment is due one interval (2 min) after LoanSet
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
8. Also found on the first run: the account-creating faucet payment counted as revenue (now ignored); `XrplChain` had no ledger clock for the referee (added); `PeriodicPayment` is fractional and rippled rounds it up, so truncating it made `LoanPay` fail with `tecINSUFFICIENT_PAYMENT` (now rounded up). If the servicer is started past the grace period it defaults the loan on its first tick, so start it right after `04_borrow.py`.
9. Checked on Devnet: a repaid Loan can still be read with `ledger_entry` (zero fields are left out) until someone sends `LoanDelete`. Release after full repayment works: referee plus borrower set the borrower's key as the regular key, then the borrower removes the signer list with it. A released lockbox cannot take a new loan, so use a fresh lockbox wallet.
10. In a closed-ended vault, LoanSet checks that the last payment date (start plus interval times payments) is at least 60 s before the redemption date. The grace period is not counted, so a late final payment can land after depositors may redeem.

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

## AWS: Lockbox auditor on AgentCore

`agent/` is a read-only AI auditor built with [Strands Agents](https://strandsagents.com). It explains referee decisions: give it a lockbox address, or ask why a payment was refused, and it reads the ledger, replays the referee's decisions against the published rulebook, and explains in plain English what happened and which rule decided each payment.

What is true today:
- It runs locally, in the terminal (`agent/main.py --ask`) and behind the dashboard's "Why?" button.
- It explains decisions; it never makes them. The referee decides with plain code in `lockbox/covenant.py`; nothing the AI says is sent back to it.
- Two places it runs, two models:
  - **Locally, behind the dashboard's "Why?" button:** the model is `gpt-oss:120b` on Ollama Cloud, through the `OLLAMA_API_KEY` in your environment. It explains the decisions on the simulated ledger.
  - **On [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-getting-started.html), us-west-2:** the Devnet auditor in `agentcore-auditor/`, with Claude Opus 5.5 on Amazon Bedrock (`us.anthropic.claude-opus-5-5`). It reads the public XRPL Devnet ledger. It uses no Ollama or Anthropic key; it runs on the Runtime's AWS role.

### On the dashboard: "Why?"

Next to the referee's latest decision, **Why?** asks the auditor to explain it in two or three sentences, labelled "AI auditor (explains, doesn't decide)". There is also an "Ask the auditor anything" box. Behind them, `POST /api/ask` takes a read-only snapshot of the current simulated ledger and the referee's decision log, and runs the agent on it with a 20 second limit. If no model key is set, or the model is slow or down, it answers `{"answer": null, "error": ...}`, the page shows a small grey "Auditor unavailable", and the rest of the demo works exactly as before.

To turn it on: `pip install -r agent/requirements.txt` in the main `.venv`, and set `OLLAMA_API_KEY` (or `ANTHROPIC_API_KEY`) in your shell or in `agent/.env`, which is gitignored. Keys come only from environment variables, never from code or committed files. `LOCKBOX_AUDITOR_MODE=offline` turns it off.

Three tools, all read-only:

| Tool | What it does |
| --- | --- |
| `get_account_history(address)` | `account_info` and `account_tx` from `https://s.devnet.rippletest.net:51234`: the lockbox's keys, and every payment it made with who signed it |
| `verify_lockbox(address)` | checks the rulebook fingerprint against the loan broker's Data field, then replays every referee decision with `lockbox/verify.py` (`replay`, `rulebook_problems`), the same code as `scripts/verify.py` and the dashboard |
| `get_rulebook()` | the published `rulebooks/lending-covenant-v1.json` and its fingerprints |

The ledger client can only call `account_info`, `account_tx`, `ledger_entry` and `server_info`. There is no signing, no submitting and no wallet. What the ledger cannot say about the past (the clock, the loan as it stood) comes from the referee's published decision log for each Devnet lockbox in `agent/data/lockboxes/`.

**Model.** The answer is written by Claude, which calls the three tools and explains each decision:

| Mode | When | Model |
| --- | --- | --- |
| `ollama` | `OLLAMA_API_KEY` is set (a free Ollama Cloud key from ollama.com, in `agent/.env`) | `gpt-oss:120b` on Ollama Cloud (`https://ollama.com`); any name from `https://ollama.com/api/tags` via `LOCKBOX_MODEL_ID` |
| `anthropic` | `ANTHROPIC_API_KEY` is set (put it in `agent/.env`, which is gitignored; see `agent/.env.example`) | Claude Opus 5.5 (`claude-opus-5-5`) through the Anthropic API, effort `medium` |
| `bedrock` | AWS credentials are available, as on AgentCore Runtime | Strands' default Bedrock model (Claude Sonnet 4.6, us-west-2) |
| `offline` | neither | no model: `agent/answer.py` writes the same facts from a fixed template |

`LOCKBOX_AUDITOR_MODE` forces one; the default `auto` takes the first available, in that order. `LOCKBOX_MODEL_ID` and `LOCKBOX_EFFORT` override the model and effort. Each answer says which mode wrote it.

### Run it locally

```bash
cd agent
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python bedrock-agentcore 'strands-agents[anthropic,ollama]' xrpl-py pytest
cp .env.example .env                 # then paste your Ollama Cloud API key into .env

.venv/bin/python main.py --ask "What did the referee do on lockbox rnC9yaPArTeyTijnNbhgGajUTd2WHr4CzG?"
.venv/bin/python main.py --ask "Why was the payment refused on r4UuCzXSK6UReG5enELvQQEwkudqnBWNXe?"

.venv/bin/python main.py            # the AgentCore Runtime contract on http://localhost:8080
curl -X POST localhost:8080/invocations -H "Content-Type: application/json" \
  -d '{"prompt": "Why was the payment refused on r4UuCzXSK6UReG5enELvQQEwkudqnBWNXe?"}'

.venv/bin/python -m pytest tests     # 21 tests, no network and no model calls
./demo.sh                            # one question and answer, for a 30-second screen recording
```

### Build, with the secret check

```bash
cd agent && .venv/bin/python build.py
```

This writes `agent/dist/`: the agent code, only the five `lockbox` modules the replay needs (`verify`, `covenant`, `buckets`, `inflows`, `units`), the rulebook and the decision logs. The build fails if it finds a `.wallets.json`, an `.env` or key file, anything shaped like an XRPL seed, a private key or an AWS key, any seed from the local `.wallets.json`, or code that can sign or submit (`chain.py`, `roles.py`, `xrpl.wallet`, `xrpl.transaction`).

### Deployed on AgentCore Runtime (us-west-2)

`agentcore-auditor/LockboxAuditor/` was made with the official AgentCore CLI (`npm install -g @aws/agentcore`, `agentcore create --template agent-python-strands`) and deployed as a CodeZip runtime named `agent`. Its `app/agent/main.py` has the same system prompt and read-only tools as `agent/`, on the public XRPL Devnet ledger. `app/agent/model/load.py` sets the Bedrock model. The template's memory resource is removed: the auditor is stateless and stores nobody's questions.

Ask the deployed auditor (needs AWS credentials for the account it is deployed in):

```bash
cd agentcore-auditor/LockboxAuditor
agentcore status
agentcore invoke --runtime agent --payload '{"prompt":"Explain what the referee did on the Lockbox account rnC9yaPArTeyTijnNbhgGajUTd2WHr4CzG from DEVNET.md, and whether every decision matched the rulebook."}'
agentcore logs runtime
```

It answers in about 30 seconds: 12 referee decisions, all signed and none refused (the loan, 7 repayments of which the ledger rejected one as `tecEXPIRED` and took two late, a withdrawal of the owner's own money, a supplier payment with invoice PO-1001, and the release), the rulebook fingerprint matching the one on the ledger, and every decision replaying the same.

To change it: edit `agent/`, run `python agentcore-auditor/sync.py` (copies the tools, the five `lockbox` modules, the decision logs and the rulebook into `app/agent/`, then runs the secret check), test with `agentcore dev` and `agentcore invoke --runtime agent --local`, then `agentcore deploy`. Clean up with `agentcore remove all --yes` then `agentcore deploy --yes`.

Kept out of git: `agentcore/.env.local` (local secrets), `agentcore/.cli/` and `agentcore/.cache/` (deployment state with the AWS account ID). `agentcore/aws-targets.json` is committed empty; the CLI fills it in locally on the first deploy. Never commit AWS keys.

## AI assistance

We built Lockbox with help from Claude (Anthropic), used through Claude Code. It wrote and reviewed code and tests with us, ran the Devnet scripts, and read the rippled source to explain ledger errors. Every design decision, rule and number in the rulebook was chosen and checked by the team, and everything was built during the event.
