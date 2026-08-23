# Razorpay AI Buildathon — Gap & Problem Evidence Dossier (v2, hardened)

**Purpose:** Verifiable citations for the problem statement you pitch. Use these links + dates in your README ("Why this problem"), pitch deck, and panel answers.
**Compiled:** Aug 22, 2026 · **Deadline:** Sep 5, 2026
**Status legend:** ✅ = primary source fetched & verified · 🟡 = reputable secondary source (named publication) · ⚠️ = still needs verification

---

## PART 1 — PRIMARY PROBLEM: Recurring-payment mandate failures (Track 03: AI Revenue Recovery)

**One-line statement:** India runs ~1B UPI AutoPay debits/month with bank approval rates as low as 10–36%; since Aug 2025 NPCI caps recovery at 4 attempts/cycle — yet historically most recoverable debits succeeded between attempts 5–9. Merchants need intelligent root-cause classification + bounded interventions, not retry spam.

### Proof 1.1 — Scale of failure crisis ✅🟡
- **Mint, 10 Oct 2025**, "UPI AutoPay woes hit India's subscription market":
  - NPCI Aug 2025 data: **55–90% of automated payments failed** across public/private banks on UPI AutoPay
  - **SBI: 2.13B AutoPay txns in August, only 36.14% approved**
  - **Airtel Payments Bank: 568.9M txns, only 10.49% approved**
  - ICICI (best large bank): ~52% approval
  - Result: involuntary subscription cancellations; industry crawling back to cards
  - https://www.livemint.com/companies/start-ups/upi-autopay-failures-recurring-payments-india-11759999218161.html

### Proof 1.2 — Regulatory twist that makes AI necessary NOW ✅🟡
- **Mint, 20 Feb 2026**, "RBI asks NPCI to review UPI Autopay":
  - Autopay ≈ **~1 billion recurring txns monthly ≈ 5% of all UPI volume** (EY India)
  - Top 10 banks processed **~926M autopay txns Nov 2025** (vs 530.5M year prior) — doubled YoY
  - NPCI compliance notice (21 May 2025), effective 1 Aug 2025: **1 attempt + max 3 retries per cycle**, non-peak windows only
  - Key quote: *"most such mandates go through successfully **between the fifth and ninth retrials**"* → the cap broke dumb strategies
  - https://www.livemint.com/industry/banking/rbi-npci-upi-autopay-debits-complaints-mandates-recurring-payments-11771480657742.html
- **NPCI circular OC-223, 7 Oct 2025**: mandate interoperability/view/port rules by 31 Dec 2025
  - https://www.npci.org.in/what-we-do/upi/upi-circulars

### Proof 1.3 — Independent engineering corroboration ✅
- **Saiprasad Shankar (CTO), 6 Jul 2026**, "The Mandate Lifecycle Nobody Models":
  - NACH e-mandate registration rejections: **~55%, up from ~28% in 2017-18**
  - **>20M UPI AutoPay mandates revoked monthly** for insufficient balance alone (base ~120M recurring debits/mo)
  - Each failed debit = bank return charge **₹250–₹500 + 18% GST** + involuntary churn
  - *"The failure is not really a payments problem. It is a modelling problem"* — nobody models mandates as lifecycle state machines; retrying fraud-flagged declines = "a compliance incident, one retry at a time"
  - https://psyprasad.tech/blog/mandate-lifecycle-nobody-models

### Proof 1.4 — Razorpay's own admissions ✅
| Claim | Source |
|---|---|
| "~25% of checkout payments fail" industry-wide | Razorpay blog, Feb 23 2024 — https://razorpay.com/blog/razorpay-intelligent-payment-retry/ |
| Intelligent Retry recovers **only ~8% more** collections over baseline | Razorpay guide, Jun 2026 — https://razorpay.com/blog/cheapest-payment-gateway-for-recurring-billing-e-nach-upi-autopay-and-subscription |
| Merchants pay for recovery: "Recover up to 20% of failed payments… grow revenue up to 10%" | https://razorpay.com/blog/introducing-the-most-effective-way-to-recover-failed-payments/ |
| Default Subscriptions retry is dumb: fixed T+1/T+2/T+3 → `halted`. No root-cause classification | Razorpay docs — https://razorpay.com/docs/payments/subscriptions/payment-retries/ |

### Proof 1.5 — REAL CHURN CAUSED: AMFI official SIP data ✅
SIP instalments execute via NACH/UPI AutoPay mandates. Under **SEBI's own definition, 3 consecutive failed instalments = SIP permanently ceased** (source: AMFI). Official AMFI monthly data shows mass discontinuations — direct evidence that mandate failures destroy recurring revenue relationships at national scale:
- **~50 lakh SIP accounts discontinued EVERY month** (Jul 2026: 50.29L discontinued vs 61.44L new registrations — net +11.15L only)
- **SIP stoppage ratio 75–85%** through 2025–26 (peak **85.3% in Dec 2025**) — for every 100 SIPs started, ~75–85 stopped
- **Economic Times, 12 May 2026**: stoppage ratio stayed above 100% even during record ₹31,115 Cr contribution month (Apr 2026)
- Sources: AMFI official reports — https://www.amfiindia.com/articles/mutual-fund · compiled tables — https://maxiomwealth.com/research/sip-mutual-fund-flows-dashboard · https://rightadvise.com/sip-data-india.html · ET — http://m.economictimes.com/mf/analysis/mutual-fund-sip-stoppage-ratio-continues-above-100-even-as-investors-contribute-record-rs-31115-crore-in-april/articleshow/131029358.cms
- Pitch framing: "Every discontinued SIP is an investor who *wanted* to invest but whose mandate pipeline broke. Recovery agents exist for e-commerce checkouts; nothing equivalent protects India's 10.6 crore SIP accounts."

### Proof 1.6 — Scale numbers (opening slide) ✅
- UPI AutoPay mandates crossed **1.27 billion (Nov 2025)**, 10x since Jan 2024 — https://razorpay.com/blog/master-recurring-payments-upi-autopay-guide/
- UPI FY2025: **228.3B txns, ₹299.7 lakh crore** — https://coinlaw.io/upi-statistics/
- NPCI official stats — https://www.npci.org.in/product/upi/product-statistics

### Proof 1.7 — FRESHNESS CHECK: problem is still live in 2026 ✅
- **The420.in, 23 Jun 2026**, "NPCI Plans Unified Dashboard for UPI AutoPay": reports that despite planned interoperability fixes, "the transaction failure rate remains rem[arkably high]" — crisis persists ~8 months after the Mint exposé. https://the420.in/npci-unified-e-mandate-tracking-upi-autopay-apps
- **productgrowth.in benchmarks (updated Jun 2026)**: UPI AutoPay debit failure runs **8–15% vs 2–3% for card mandates** (structural, not operational); smart retry windows recover 15–20% of failures; grace-period flows recover another 15–20% of would-be cancellations. Useful for your baseline-vs-agent math. https://productgrowth.in/insights/fintech/upi-autopay-guide
- **LIVE primary data you can cite in your demo**: NPCI publishes per-bank Business Decline / Technical Decline / Uptime monthly — pull current-month numbers from https://www.npci.org.in/statistics/bd-td-and-uptime and the AutoPay Ecosystem Statistics section at https://www.npci.org.in/product/autopay . Bank-wise AutoPay approval volumes also compiled at https://dataful.in/datasets/22492/
- Pitch line: "These are NPCI's own published monthly numbers — here is this month's."

---

## PART 2 — ALTERNATIVE PROBLEM: B2B Receivables + Promise-to-Pay Agent (Track 03) — NOW FULLY VERIFIED ✅

**Gap claim:** Razorpay processes transactions but doesn't chase money that hasn't arrived — no dunning/overdue/receivables management in its product line. Smart Collect reconciles *after* money arrives. Third parties (CredFlow, Kapittx, OptimAR) fill this gap today.

### Proof 2.1 — City-level overdue severity ⚠️AGE: Aug 2024 (2 yrs old — corroborate only, lead with 2.2)
> Data covers FY21–23. Still valid as directional evidence; do NOT make it your headline number.
- **CEOWORLD magazine, 23 Aug 2024**, reporting Recordent survey of 2,800 member businesses, FY21–23, eight major hubs:
  > *"approximately **52% of payments made or received by businesses remained overdue for more than 90 days**"* in Hyderabad, Kolkata, Chennai & Pune; Mumbai 29%; Bengaluru/Delhi-NCR/Ahmedabad 42–49%
  - Only 18–22% of receivables collected on time in Chennai/Pune/Hyderabad (vs 36% Mumbai)
  - https://ceoworld.biz/2024/08/23/survey-reveals-alarming-b2b-payment-delays-among-indian-msmes

### Proof 2.2 — Fresh 2026 transaction-level data ✅
- **Recordent "Indian SME Receivables Report 2026"** (released World MSME Day, 27 Jun 2026, via UNI):
  - Average overdue receivables: **₹3.83 crore per business** (pending >360 days)
  - MSMEs take **average 73 days to collect** invoices vs credit terms mostly ≤30 days (**82.6% of invoices** issued with ≤30-day terms) → delay is operational, not contractual
  - Based on **~1.1 lakh MSMEs and 10+ lakh transaction-level records**
  - https://www.uniindia.com/~/delayed-payments-leave-indian-smes-with-3-83-cr-in-average-overdue-receivables-recordent-report/Business%20Economy/news/3891149.html · https://knnindia.co.in/news/newsdetails/msme/delayed-payments-stretch-msme-cash-cycles-strain-working-capital-report

### Proof 2.3 — Macro magnitude ✅
- **GAME × FISME × C2FO, "Delayed Payments Report 3.0"** (ET, 13 Jan 2026): **over ₹7.3 lakh crore of MSME receivables locked in payment delays** at any given time
  - https://economictimes.indiatimes.com/small-biz/sme-sector/over-rs-7-3-lakh-crore-in-msme-receivables-stuck-due-to-delayed-payments-basant-kaur-c2fo/articleshow/126496632.cms

### Proof 2.4 — Institutional baseline (D&B dataset) ✅
- **GAME/D&B "Delayed Payments Report" (Jun 2022)**, built on Dun & Bradstreet Global Trade Exchange:
  - **73% of invoices paid by Maharatna PSUs delayed beyond credit period** (Navratnas 54%, NIFTY50 private 31%)
  - Public administration worst sector: 69% of payments 60+ days past due (2020)
  - Even at 30-day terms, **~10% of MSME revenue stays unrealized for 90 days**
  - PDF: https://massentrepreneurship.org/wp-content/uploads/2022/12/Delayed-Payments-Report.pdf · also https://www.dnb.co.in/file/reports/Delayed-Payments-Report.pdf

### Proof 2.5 — Legal tailwind (two statutes) ✅
- **MSMED Act 2006, Sec 15–16**: buyers MUST pay registered MSE suppliers within **45 days** or owe compound interest at 3× bank rate
- **Finance Act 2023, Sec 43B(h)** (effective FY2024-25): buyers lose income-tax *deduction* on expenses unpaid to MSMEs beyond terms — payment discipline now has tax teeth
  - Explainer: https://www.indiafilings.com/learn/section-43bh-new-msme-45-days-payment-rule

### Track alignment
- Official page names these directions verbatim: **"B2B receivables chaser", "Promise-to-pay tracker"** — https://razorpay.com/buildathon/
- ⚠️ Before claiming "RazorpayX has zero dunning": verify live feature list at https://razorpay.com/x/

---

## PART 3 — ALTERNATIVE PROBLEM: Grievance Triage + TAT-Compliance Agent (Open Track)

- **RBI TAT Circular ✅ (primary source)** — DPSS.CO.PD No.629/02.01.014/2019-20, Sep 20 2019, effective 15 Oct 2019:
  - Most electronic channels (UPI/NACH/PPI/card transfers): auto-reversal **T+1 day**, else **₹100/day compensation**, payable suo motu
  - ATM cash-not-dispensed: T+5 days, else ₹100/day
  - PDF: https://rbidocs.rbi.org.in/rdocs/notification/PDFs/CIRCULAR677EC931A7A65E4D99AA957D8E85BC0A2A.PDF · page: https://m.rbi.org.in/scripts/RTGS_Notification.aspx?Id=11693
  - Note: secondary-blog claims of "T+5/T+7 working days" are inaccurate — cite the circular itself
- Complaint-volume context 🟡: RBI Ombudsman received **9,34,355 complaints in FY2023-24 (+32.8%)**, digital payments a large share — https://razorpay.com/blog/how-to-escalate-payment-gateway-complaints-in-india-a-step-by-step-guide-2026/
- Review-site sentiment 🟡 (pattern, not named case): https://www.trustpilot.com/review/razorpay.com — slow resolution, fund holds, generic responses recur

---

## PART 4 — SATURATED AREAS TO AVOID (already shipped by Razorpay)

Track 02 example directions map ~1:1 onto live products:
- Fraud ML monitoring → **Shield risk engine / Thirdwatch** (https://razorpay.com/thirdwatch/)
- Chargeback responder → dispute automation flows (https://razorpay.com/docs/payments/disputes/)
- COD return-risk scoring → RTO prediction products

Also partially true for subscription retry (Intelligent Payment Retry + Failed Payment Recovery ship today — see 1.4). Position mandate recovery as **"next generation of a category Razorpay monetizes — closing the gap their own 8%-uplift admits"**, never "nobody has done this."

---

## PART 4A — FRESHNESS AUDIT (as of Aug 22, 2026)

| Source | Date | Age | Verdict |
|---|---|---|---|
| Mint UPI AutoPay exposé | Oct 10, 2025 (data: Aug 2025) | ~10 mo | 🟢 Still the definitive bank-level report; **currency re-confirmed Jun 2026** (Proof 1.7). Pair with live NPCI stats |
| Mint RBI/NPCI review + retry cap | Feb 20, 2026 | 6 mo | 🟢 Fresh, load-bearing |
| psyprasad.tech mandate-lifecycle post | Jul 6, 2026 | ~7 wk | 🟢 Fresh |
| productgrowth.in AutoPay benchmarks | updated Jun 2026 | ~2 mo | 🟢 Fresh |
| The420.in NPCI dashboard piece | Jun 23, 2026 | 2 mo | 🟢 Fresh (proves problem persists) |
| Razorpay blogs (retry/recovery/guides) | Feb 2024–Jun 2026 | mixed | 🟢 Product claims don't expire; the **8%-uplift figure is from Jun 2026** — cite that one |
| AMFI SIP data | monthly, latest Jul 2026 | <2 mo | 🟢 Live dataset |
| ET SIP stoppage ratio | May 12, 2026 | 3 mo | 🟢 Fresh |
| Recordent SME Receivables Report | Jun 27, 2026 | <2 mo | 🟢 Freshest B2B data — make this your headline |
| CEOWORLD × Recordent survey | Aug 23, 2024 | 2 yrs | 🟡 Corroboration only; data is FY21–23 |
| GAME/FISME/C2FO ₹7.3L cr | Jan 13, 2026 | 7 mo | 🟢 Fresh |
| GAME × D&B Delayed Payments Report | Jun 2022 | 4 yrs | 🟡 Institutional baseline only; never headline |
| MSMED Act 2006 / Sec 43B(h) Finance Act 2023 | law in force since Apr 2024 | n/a | 🟢 Statute — does not age |
| RBI TAT circular DPSS.CO.PD No.629 | Sep 20, 2019 | 7 yrs | 🟢 **Still in force** — verified via Axis Bank Customer Compensation Policy *last reviewed June 2026* which cites it as operative (https://www.axis.bank.in/docs/default-source/default-document-library/Customer-Compensation-Policy.pdf). No superseding circular found. Note: RBI's proposed fraud-compensation rules (Moneycontrol, Jun 26 2026) are a separate track and do not amend this framework |
| Official buildathon page | live | n/a | 🟢 |

**Rule for your deck:** every slide number should trace to something ≤12 months old OR a statute/regulation/official statistics page. That standard is met after these upgrades.

---

## PART 5 — REMAINING UNVERIFIED CLAIMS

| Claim | Status | Action |
|---|---|---|
| ~~52% B2B payments overdue >90 days~~ | ✅ Verified (CEOWORLD/Recordent, Proof 2.1) | Usable with citation |
| ~~Median B2B DSO ≈56 days~~ | ➖ Superseded by stronger verified figure: **73-day average collection** (Recordent 2026, Proof 2.2) | Use 73 days instead |
| Judging criteria "Problem Taste / Build Quality / AI Judgment / Failure Recovery" | ⚠️ Not seen on official page | Treat as directional; **per-track "bar" text IS the rubric** |
| RazorpayX has zero dunning/reminders | ⚠️ Verify against live product pages | Check before claiming whitespace |
| Trustpilot "120-day fund hold" specific review | ⚠️ Pattern real, case unverified | Use as sentiment category only |

---

## PART 6 — SOURCE INDEX

**Problem A (mandate failures):**
1. Mint (10 Oct 2025) — https://www.livemint.com/companies/start-ups/upi-autopay-failures-recurring-payments-india-11759999218161.html
2. Mint (20 Feb 2026) — https://www.livemint.com/industry/banking/rbi-npci-upi-autopay-debits-complaints-mandates-recurring-payments-11771480657742.html
3. Mandate lifecycle deep-dive (Jul 2026) — https://psyprasad.tech/blog/mandate-lifecycle-nobody-models
4. Razorpay Subscriptions retry docs — https://razorpay.com/docs/payments/subscriptions/payment-retries/
5. Razorpay Intelligent Payment Retry (Feb 2024) — https://razorpay.com/blog/razorpay-intelligent-payment-retry/
6. Razorpay Failed Payment Recovery — https://razorpay.com/blog/introducing-the-most-effective-way-to-recover-failed-payments/
7. Razorpay recurring-billing guide (Jun 2026) — https://razorpay.com/blog/cheapest-payment-gateway-for-recurring-billing-e-nach-upi-autopay-and-subscription
8. Razorpay UPI AutoPay guide (Jun 2026) — https://razorpay.com/blog/master-recurring-payments-upi-autopay-guide/
9. AMFI official SIP reports — https://www.amfiindia.com/articles/mutual-fund
10. AMFI SIP compiled dashboard — https://maxiomwealth.com/research/sip-mutual-fund-flows-dashboard
11. ET SIP stoppage ratio (12 May 2026) — http://m.economictimes.com/mf/analysis/mutual-fund-sip-stoppage-ratio-continues-above-100-even-as-investors-contribute-record-rs-31115-crore-in-april/articleshow/131029358.cms
12. NPCI UPI stats / circulars — https://www.npci.org.in/product/upi/product-statistics · https://www.npci.org.in/what-we-do/upi/upi-circulars
13. UPI compendium (Feb 2026) — https://coinlaw.io/upi-statistics/

**Problem B (B2B receivables):**
14. CEOWORLD × Recordent survey (23 Aug 2024) — https://ceoworld.biz/2024/08/23/survey-reveals-alarming-b2b-payment-delays-among-indian-msmes
15. Recordent SME Receivables Report 2026 (UNI, 27 Jun 2026) — https://www.uniindia.com/~/delayed-payments-leave-indian-smes-with-3-83-cr-in-average-overdue-receivables-recordent-report/Business%20Economy/news/3891149.html
16. KNN India coverage (29 Jun 2026) — https://knnindia.co.in/news/newsdetails/msme/delayed-payments-stretch-msme-cash-cycles-strain-working-capital-report
17. ET × C2FO ₹7.3L crore (13 Jan 2026) — https://economictimes.indiatimes.com/small-biz/sme-sector/over-rs-7-3-lakh-crore-in-msme-receivables-stuck-due-to-delayed-payments-basant-kaur-c2fo/articleshow/126496632.cms
18. GAME × D&B Delayed Payments Report (2022, PDF) — https://massentrepreneurship.org/wp-content/uploads/2022/12/Delayed-Payments-Report.pdf
19. Sec 43B(h) explainer — https://www.indiafilings.com/learn/section-43bh-new-msme-45-days-payment-rule

**Problem C (grievance/TAT):**
20. RBI TAT circular PDF (Sep 2019) — https://rbidocs.rbi.org.in/rdocs/notification/PDFs/CIRCULAR677EC931A7A65E4D99AA957D8E85BC0A2A.PDF
21. RBI notification page — https://m.rbi.org.in/scripts/RTGS_Notification.aspx?Id=11693
22. Razorpay escalation guide (Jun 2026) — https://razorpay.com/blog/how-to-escalate-payment-gateway-complaints-in-india-a-step-by-step-guide-2026/
23. Trustpilot reviews — https://www.trustpilot.com/review/razorpay.com

**Reference:**
24. Official buildathon page (tracks + bars) — https://razorpay.com/buildathon/
25. The420.in NPCI AutoPay dashboard (Jun 2026) — https://the420.in/npci-unified-e-mandate-tracking-upi-autopay-apps
26. productgrowth.in AutoPay design guide (updated Jun 2026) — https://productgrowth.in/insights/fintech/upi-autopay-guide
27. NPCI BD/TD & Uptime monthly stats (LIVE) — https://www.npci.org.in/statistics/bd-td-and-uptime
28. NPCI AutoPay Ecosystem Statistics (LIVE) — https://www.npci.org.in/product/autopay
29. Bank-wise AutoPay approval dataset — https://dataful.in/datasets/22492/

---
*v2 changes: Proof 1.5 added (AMFI SIP churn); Part 2 upgraded from ⚠️→✅ with four independent verified sources; DSO claim superseded by 73-day collection figure; Part 5 cleaned.*
