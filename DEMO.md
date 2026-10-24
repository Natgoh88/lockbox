# Lockbox: 3-minute live demo

One person drives the dashboard, one person talks. Total: 180 seconds.

## Before you go on stage

- Start the dashboard: `python scripts/offline_demo.py`, open http://localhost:8403, click **Reset**.
- Check the header says **simulated ledger**. (The Revenue and Servicer toggles only exist in Devnet mode, so there is nothing else to set.)
- Open a terminal in the repo, ready to run `python scripts/verify.py`. Run it once beforehand so it is warm.
- Open these Devnet tabs from DEVNET.md, in this order:
  1. [LoanBrokerSet](https://devnet.xrpl.org/transactions/8BC076B34D27FDDBF303B5134C06542AE5D52E4BDEC60D16FE2D51672B8F1DC4): the broker's Data field holds the rulebook fingerprint
  2. [LoanSet](https://devnet.xrpl.org/transactions/08C3E4572B95DBE4DDE1AE8AAEDE43A4E4A27C52CD85C346AED10E2D3E23CA5A): the agreement hash in Data, signed by borrower plus referee
  3. [Supplier payment](https://devnet.xrpl.org/transactions/9BA6C4123389BC21DFDC2544E77CE9F716274FF7BDE0BE26E282B9EA5F222C36): 20 XRP with invoice PO-1001 in the memo
  4. [Final repayment](https://devnet.xrpl.org/transactions/B80FD8747D54985C987B85269E6868C1AD67ABE297E0D09DE216D14F8E099CB1): LoanPay 6 of 6
  5. [Release step 1](https://devnet.xrpl.org/transactions/150B823E889725379A0952086EF28281542721ECC97B3DA71324516F8E174AE6) and [step 2](https://devnet.xrpl.org/transactions/0817045C8C26D8B5EA1D629923BB94FE158758475009DF75E82E49EB3ADA23DE): the account goes back to the borrower
  6. [Default](https://devnet.xrpl.org/transactions/1792A17BAD9881CEDB33C62DA2AB32C34B9DF74145F63C9604BA64527A871EB8): first-loss cover pays before depositors
  7. The good-path verify table in DEVNET.md ("verify.py --devnet, good path"): every referee decision matched
- Have the backup video open in another tab, paused at the start.

## The script

| # | Seconds | What to click | What to say |
| --- | --- | --- | --- |
| 1 | 10 | Nothing, dashboard on screen | "Small exporters get paid by foreign buyers but can't borrow against that revenue, because a lender has no way to stop the money walking out of the account." |
| 2 | 20 | Point at the revenue history, then **1. Lock the account** | "This is a real payment history: buyers paying invoices. Locking turns the account into a multi-signed account. Our referee has to sign every payment, plus the borrower or the lender, and the master key is switched off." |
| 3 | 25 | **2. Underwrite**, point at the two quotes and the stress line | "The loan is sized from that history, and every number is explained. Same business, two prices: with the lockbox and without it. The unlocked price is our assumption, not market data. And here is what happens if revenue drops 30 percent." |
| 4 | 15 | **3. Take the loan** | "The borrower signed the exact terms. The hash of those terms and of the rulebook are both written on the ledger." |
| 5 | 15 | **Send loan money to own wallet** | "Now the owner tries to take the loan money home. Refused. Loan money can only go to approved suppliers." |
| 6 | 15 | **Pay approved supplier** | "Paying the supplier on the invoice is allowed, up to the approved amount, with the invoice number on the payment." |
| 7 | 30 | **Advance 10 min, buyers stop paying**, then **Try to withdraw everything** | "Sales stop. Repayment comes first, so the account freezes on ledger facts: a payment is past due. If cash came back the servicer would take a late payment first. There is none, so the loan defaults. First-loss cover pays the depositors before they lose anything. And the owner still can't empty the account." |
| 8 | 30 | Terminal: `python scripts/verify.py` | "Why trust the referee? Anyone can check it. This takes the published rulebook, checks its fingerprint against the one on the ledger, then replays every decision the referee made from the ledger history. Every row says matches: yes." |
| 9 | 20 | The Devnet tabs, in order | "And this is not only a simulation. On XRPL Devnet: the rulebook fingerprint on the broker, the loan with the signed agreement hash, loan money paid to the supplier, the last repayment, the account handed back, a default where cover paid first, and verify showing every referee decision matched." |

If there is time left over, run `python scripts/verify.py --tamper-verdict` and show the row that says NO.

## If something breaks

- **Dashboard does not load or hangs:** switch to the backup video tab and talk over it with the same script. Don't debug on stage.
- **A click gives an unexpected result:** say "let me show you the recording of this step" and play the video from that step.
- **verify.py fails to start:** show the screenshot of a clean run (take one before the demo) and run `--tamper-verdict` later in Q&A if asked.
- **Devnet explorer is slow or down:** use the screenshots of each explorer page saved next to the video. The dashboard demo is on the simulated ledger and says so on screen.

## Q&A

**What happens if a loan would still be in its grace period when depositors can redeem?**
The ledger's own check only looks at the last due date. Our referee also adds the grace period, and refuses the loan if the last due date plus grace runs past the vault's redemption date. Depositors never have to wait on a loan that is still in grace.

**Does a late payment go straight to default?**
No. Once a payment is past due, the servicer marks the loan impaired and keeps trying a late payment during the grace period. Even after grace, if the cash is there, it tries one more late payment first. It only defaults if that fails or the cash isn't there.

**Why did the Devnet default only happen after the owner took money out?**
On Devnet the default only happens after the owner withdraws their own money, because the lockbox keeps paying the lender while there is money in it. That's the rules working, not a bug.

**Where are the refused payments on the ledger?**
A refused payment never appears on the ledger, because the referee simply doesn't sign. Refusals show up in the referee's decision feed and the verify table.

**Can the referee steal the money?**
No. The referee has weight 2 and the quorum is 3, so it always needs the borrower or the lender as well. Borrower and lender together can't move money without the referee either.

**What if the lender marks the loan impaired to pressure a healthy borrower?**
That doesn't freeze anything. The freeze only uses ledger facts: a payment past due, or revenue below the installment two intervals in a row.

**What if the referee goes offline?**
Money stays put until it comes back, which protects both sides. The next step is three referees with weighted keys, so one going down doesn't stop anything.

**How do I know the referee followed its rules?**
The rulebook fingerprint is in the loan broker's Data field on the ledger, and the agreement hash is in the loan's Data field. `scripts/verify.py` checks the published rulebook file against both, then replays every decision.

**Is the interest real?**
On a loan that lasts minutes, interest is a few drops. In the demo the lender's income is the service fee. A real loan runs for months.

**What is real and what is simulated?**
The dashboard runs on a simulated ledger and says so on screen. The Devnet links show the same flow on the real XRP Ledger Devnet, with test XRP.
