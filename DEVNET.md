# Lockbox on XRPL Devnet

Everything below happened on the real XRP Ledger Devnet with test XRP, on 24 October 2026. Each link opens the transaction in the Devnet explorer. The dashboard demo runs on a simulated ledger and says so on screen; this page is the proof that the same flow works on the real ledger.

Network: `https://s.devnet.rippletest.net:51234` (rippled 3.4.1, SingleAssetVault, LendingProtocol and LendingProtocolV1_1 enabled). Rulebook: `lending-covenant` v1, fingerprint `4F1934812BE51C9281D1623C1C670F244277B5B96A71C99170F8B3E613737101`, published in `rulebooks/lending-covenant-v1.json`.

A refusal never reaches the ledger: when the referee says no, it does not sign, so there is no transaction to link. The refusals are in the referee's log, and `scripts/verify.py --devnet` replays them below.

## Good path: borrow, pay the supplier, repay, release

Lockbox `rnC9yaPArTeyTijnNbhgGajUTd2WHr4CzG`. The loan was serviced by the dashboard in Devnet mode (`LOCKBOX_MODE=devnet`) and by the servicer script.

| Transaction | What it shows |
| --- | --- |
| [32FFA1B9](https://devnet.xrpl.org/transactions/32FFA1B960995831244FA9D01466D3CB9E411DE051305A08561AEBF2B5543730) | VaultCreate: a closed-ended XRP vault (deposits for 5 minutes, then loans, then depositors redeem 75 minutes later) |
| [62FB382B](https://devnet.xrpl.org/transactions/62FB382B9941A91B97BAD8C95E6FB5AB817F7A586C61D95A53C546137645D976) | VaultDeposit: the lender puts 50 XRP of depositor money in the vault |
| [8BC076B3](https://devnet.xrpl.org/transactions/8BC076B34D27FDDBF303B5134C06542AE5D52E4BDEC60D16FE2D51672B8F1DC4) | LoanBrokerSet: the broker's Data field holds the rulebook fingerprint 4F1934812BE51C92... |
| [B5F77045](https://devnet.xrpl.org/transactions/B5F770459B954150F6561C54686B1D8675AF5203B9161FBDAD9D5C071F430560) | LoanBrokerCoverDeposit: 15 XRP of first-loss cover |
| [19E8FC9D](https://devnet.xrpl.org/transactions/19E8FC9D844CB65FF04F9E1A160851B706B480DDC06348D4257356BCCEADBC4B) | SignerListSet: the lockbox's signers, referee weight 2, borrower 1, lender 1, quorum 3 |
| [D2A1378B](https://devnet.xrpl.org/transactions/D2A1378B4D08A031AF2C30380062361A234284362FB897134E815D2CAACE3DC4) | AccountSet: the lockbox's master key switched off |
| [08C3E457](https://devnet.xrpl.org/transactions/08C3E4572B95DBE4DDE1AE8AAEDE43A4E4A27C52CD85C346AED10E2D3E23CA5A) | LoanSet: 15.47 XRP, tier A. Counterparty signed by borrower plus referee. Data holds the agreement hash 39CDAC58840B9AA8... |
| [18C12DB2](https://devnet.xrpl.org/transactions/18C12DB2D65421347E0AA2035C81516782A30040BAC61A5914DD855475E3EC59) | LoanPay rejected with tecEXPIRED: sent just before the due date but validated after it |
| [BFC041F6](https://devnet.xrpl.org/transactions/BFC041F6554575ABAA21EA64416C1A827B4D683CA25DCEECC174D1B8621E60E9) | LoanManage impair: the servicer marks the late installment |
| [CBD72567](https://devnet.xrpl.org/transactions/CBD72567DB85F18234591F16C30E190B2E680BD0037A488FABB68F6E20B4787A) | LoanPay 1 of 6, late (tfLoanLatePayment), signed by lender plus referee |
| [0DDD7C10](https://devnet.xrpl.org/transactions/0DDD7C106AE61D2443533C0E3376B0F704EDDEFF8CAC1ADDC6DE839B3E13F050) | LoanPay 2 of 6, on time |
| [31299B18](https://devnet.xrpl.org/transactions/31299B18524B31A7BABDBFCA5595CDCBD561A131887790CD5EEFBFACD843ED0B) | Payment 15.47 XRP to the owner's wallet: allowed, because it came out of the owner's own money (buyer revenue plus the opening deposit), not the loan |
| [1B72EDC0](https://devnet.xrpl.org/transactions/1B72EDC0BE49A585FC2248A0A57CFD5BC0B59269632594A4A284837C27C38238) | LoanManage impair: installment 3 went late while a dashboard click held the servicer |
| [38000E28](https://devnet.xrpl.org/transactions/38000E287A73AC0C77E489088E1C1E2309CC0E7FA9CC6C86076FB8672794890D) | LoanPay 3 of 6, late |
| [ABD32DB1](https://devnet.xrpl.org/transactions/ABD32DB195413EBCE00C400605E863BFD83BECEE2A72A4B7EA05D7E60486C8EA) | LoanPay 4 of 6, on time |
| [9BA6C412](https://devnet.xrpl.org/transactions/9BA6C4123389BC21DFDC2544E77CE9F716274FF7BDE0BE26E282B9EA5F222C36) | Payment 20 XRP to the approved supplier with invoice PO-1001 in the memo: loan money spent on what it was for |
| [A1F3848B](https://devnet.xrpl.org/transactions/A1F3848B101BC913F6DBFA8F3BED159E09C6CDE789C1035C78293A78A98B596A) | LoanPay 5 of 6, on time |
| [B80FD874](https://devnet.xrpl.org/transactions/B80FD8747D54985C987B85269E6868C1AD67ABE297E0D09DE216D14F8E099CB1) | LoanPay 6 of 6, on time: the loan is fully repaid |
| [150B823E](https://devnet.xrpl.org/transactions/150B823E889725379A0952086EF28281542721ECC97B3DA71324516F8E174AE6) | SetRegularKey: release step 1, referee plus borrower hand the account to the borrower's key (referee rule nothing_owed) |
| [0817045C](https://devnet.xrpl.org/transactions/0817045C8C26D8B5EA1D629923BB94FE158758475009DF75E82E49EB3ADA23DE) | SignerListSet removal: release step 2, signed with the borrower's new regular key. The account is the borrower's again |

Not on the ledger, because the referee refused it: the dashboard's "Send loan money to own wallet" on the default-path lockbox below (row 2 of its verify table).

### verify.py --devnet, good path

```
Verifying the referee on Devnet
lockbox rnC9yaPArTeyTijnNbhgGajUTd2WHr4CzG

1. Rulebook lending-covenant v1, fingerprint 4F1934812BE51C92...
   on the ledger (broker Data): 4F1934812BE51C92...
   file matches the ledger: yes

2. Replaying 12 referee decision(s) from the ledger history
  #  transaction                        verdict  deciding rule                matches
-------------------------------------------------------------------------------------
  1  LoanSet 15.47 XRP                  allowed  broker_rulebook_hash         yes
  2  LoanPay 2.63 XRP                   allowed  covered_by_balance           yes
  3  LoanPay 2.63 XRP                   allowed  covered_by_balance           yes
  4  LoanPay 2.63 XRP                   allowed  covered_by_balance           yes
  5  Payment 15.47 XRP to owner wallet  allowed  within_free_cash             yes
  6  LoanPay 2.63 XRP                   allowed  covered_by_balance           yes
  7  LoanPay 2.63 XRP                   allowed  covered_by_balance           yes
  8  Payment 20.00 XRP to rpABm5e6...   allowed  within_principal             yes
  9  LoanPay 2.63 XRP                   allowed  covered_by_balance           yes
 10  LoanPay 2.63 XRP                   allowed  covered_by_balance           yes
 11  SetRegularKey                      allowed  nothing_owed                 yes
 12  SetRegularKey                      allowed  nothing_owed                 yes

What the deciding rules say:
  broker_rulebook_hash         the loan is under this rulebook (fingerprint on the ledger) and matches the signed terms
  covered_by_balance           repayment comes first, as long as the fee reserve stays
  within_free_cash             only earned free cash (and the owner's own deposit) may go to the owner's wallet
  within_principal             loan money may go to an approved supplier, up to its cap, with the invoice memo

OK: the rulebook matches the ledger and all 12 decisions follow from it
```

Rows 11 and 12 are the same release decision: a dry run of the referee's review before the real one.

## Default path: owner takes its money out, buyers stop paying

Lockbox `r4UuCzXSK6UReG5enELvQQEwkudqnBWNXe`, same vault and broker as the good path. Buyers stopped paying after the loan. The owner first tried to take the loan money home (refused), paid the approved supplier (allowed), then took out its own money (allowed). The referee had held back the next installment, which paid installment 1. Installment 2 could not be paid, the loan was impaired, and after the grace period the lender defaulted it. The servicer did not try one more late payment because the lockbox only held its ledger reserve.

| Transaction | What it shows |
| --- | --- |
| [24A470E2](https://devnet.xrpl.org/transactions/24A470E26A33B4B720840C62F70BB377FE0A2C068D131595BAB654654BF13C02) | SignerListSet: a fresh lockbox locked with the same weights |
| [2014F435](https://devnet.xrpl.org/transactions/2014F435589BF4EAA8CC875F09FC88F5FA784DB84212EC28BCE1893441DA832B) | AccountSet: master key off |
| [07EDFE95](https://devnet.xrpl.org/transactions/07EDFE956DAA3A64D07BCE7C9FD0447785533A22CEEA594E0EAE00180A0C2089) | LoanSet: 13.09 XRP, tier A, agreement hash 1DEB659EC0D1AEED... in Data |
| [D30EE586](https://devnet.xrpl.org/transactions/D30EE586D876D6D2A5F5678DF49BFBA79AA7DE753F394FD700E6320B35584FB6) | Payment 20 XRP to the approved supplier with invoice PO-1001 |
| [3F79E96D](https://devnet.xrpl.org/transactions/3F79E96D388DD7AB44FA95256960D4BC812E59C42D4C1A9DE1BFFA58765D8700) | Payment 121.30 XRP to the owner's wallet: the owner takes out its own money (allowed). Only the next installment stays behind |
| [0E9BA38B](https://devnet.xrpl.org/transactions/0E9BA38B8865D811CA2354EB85504D16CD994BEB9E633968B72EC1EEB1C74626) | LoanPay rejected with tecEXPIRED: validated just after the due date |
| [F2FE95FD](https://devnet.xrpl.org/transactions/F2FE95FD6940B272796E3DB6002B10492538185BCEDFF5547CAD034652B8EEFA) | LoanManage impair |
| [F48E38F7](https://devnet.xrpl.org/transactions/F48E38F7061A76DAF367AF458AF02B3AC2BC691AB339B1057B7AA6DC52D7C133) | LoanPay 1 of 6, late, paid from the installment the referee held back |
| [9524466B](https://devnet.xrpl.org/transactions/9524466BDCC37FF48D1CA175C59768D9DD8803A1202529CD5EE95470808B1D75) | LoanManage impair: installment 2 cannot be paid, buyers stopped paying |
| [1792A17B](https://devnet.xrpl.org/transactions/1792A17BAD9881CEDB33C62DA2AB32C34B9DF74145F63C9604BA64527A871EB8) | LoanManage default after the grace period: first-loss cover pays 2.73 XRP (15.00 to 12.27), depositors take the rest (vault 50.00 to 41.82) |

### verify.py --devnet, default path

```
Verifying the referee on Devnet
lockbox r4UuCzXSK6UReG5enELvQQEwkudqnBWNXe

1. Rulebook lending-covenant v1, fingerprint 4F1934812BE51C92...
   on the ledger (broker Data): 4F1934812BE51C92...
   file matches the ledger: yes

2. Replaying 6 referee decision(s) from the ledger history
  #  transaction                        verdict  deciding rule                matches
-------------------------------------------------------------------------------------
  1  LoanSet 13.09 XRP                  allowed  broker_rulebook_hash         yes
  2  Payment 141.30 XRP to owner wallet refused  within_free_cash             yes
  3  Payment 20.00 XRP to rPW1wZxZ...   allowed  within_principal             yes
  4  Payment 121.30 XRP to owner wallet allowed  within_free_cash             yes
  5  LoanPay 2.23 XRP                   allowed  covered_by_balance           yes
  6  LoanPay 2.23 XRP                   allowed  covered_by_balance           yes

What the deciding rules say:
  broker_rulebook_hash         the loan is under this rulebook (fingerprint on the ledger) and matches the signed terms
  within_free_cash             only earned free cash (and the owner's own deposit) may go to the owner's wallet
  within_principal             loan money may go to an approved supplier, up to its cap, with the invoice memo
  covered_by_balance           repayment comes first, as long as the fee reserve stays

OK: the rulebook matches the ledger and all 6 decisions follow from it
```

## Earlier run, first rulebook

The first Devnet loan, before the rulebook fingerprint and the new referee rules. It was fully repaid and then released.

| Transaction | What it shows |
| --- | --- |
| [90811E10](https://devnet.xrpl.org/transactions/90811E10D1FA9308D92A1850AEF266798771F244EEE0467147B3D10791937CD3) | LoanSet on the first rulebook: 12.58 XRP, tier A |
| [1F9259D5](https://devnet.xrpl.org/transactions/1F9259D5177F1A0E30DF608B0672ECA8320F44690ABF5209F89B68B7F1D54506) | First LoanPay of that loan, on time. All six were paid |
| [1485EC6F](https://devnet.xrpl.org/transactions/1485EC6FF1B13A3D74CD29BD489E8377749B0A09E5EB0CDC826A62821115E47D) | SetRegularKey: first release on Devnet, step 1 |
| [78812A4A](https://devnet.xrpl.org/transactions/78812A4A7A7D648C357B32AF1EFFFA251A0EDC6FB42146E5A3E33630D3C4723E) | SignerListSet removal: first release on Devnet, step 2 |

## What the first runs taught us

- LendingProtocolV1_1 only lets a loan broker sit on a closed-ended vault (`tecNO_PERMISSION` otherwise).
- `PeriodicPayment` is fractional and rippled rounds it up, so an installment must be rounded up to whole drops or LoanPay fails with `tecINSUFFICIENT_PAYMENT`.
- A LoanPay sent 30 seconds before the due date can validate after it and fail with `tecEXPIRED`; the servicer then pays it as a late payment. A bigger lead avoids this.
- The payment that created an account (here a faucet payment) is the owner's own money, not revenue.
- A repaid Loan can still be read with `ledger_entry` until someone deletes it.

## Mainnet read-only scoring

`python scripts/06_score_address.py rEb8TK3gBgk5auZkwc6sHnwrGVJH8DuaLh --interval-days 1`, run on 24 October 2026 against `https://xrplcluster.com`. XRPScan labels the address as Binance (binance.com, verified). It is an older Binance wallet that now holds about 100 XRP: a public business address, not a private person's. Nothing was signed or sent: the script only reads `account_tx`.

```
289 incoming XRP payments found, 400 transactions read
approved=False tier=C score=52
revenue per interval (oldest to newest, XRP): [0.0, 9.9, 0.0, 0.0, 0.0, 0.0, 0.0, 512.37, 0.0, 0.0, 15.88, 407.37]
  - history 11 of 12 intervals: 23/25
  - inflow volatility (std/mean) 2.07: 0/35
  - 4 paying sender(s), largest share 66%: 9/20
  - last interval at 474% of the average: 20/20
  - sender rDrtNsGeAoUWa6Hu12yGBPtLRMESRoyS5W is 66% of revenue, counted at 40%
  - average inflow 85.9558 XRP per interval, conservative 0.0000 XRP
  - installment kept at or below conservative inflow / 2
  - tier C: rate 14.0% a year
  - conservative inflow is zero
```

The underwriter declines it: the XRP arriving at this wallet is lumpy (most days close to zero, then 512 XRP in one day), and one sender is two thirds of it. A cautious lender would not size a loan from that, and the rules say so line by line. How many transactions the public node returns per run varies with its paging (one run read 800, another 400); the verdict and the per-day revenue were the same.
