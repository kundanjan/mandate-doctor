# Data Calibration Sources

This directory contains frozen snapshots of first-party data used to calibrate
the evaluation simulator. All data is free and sourced directly from NPCI.

## Snapshot 1: Remitter-Bank Mandate Execution

**File:** `npci-autopay-execution-2026-07.csv`

| Field | Value |
|---|---|
| Source page | https://www.npci.org.in/product/ecosystem-statistics/autopay |
| Source API | `GET /api/ecosystem-statistics/get-statistics?product_name=Autopay&tab_name=top50-remitter&type_name=execution&year=2026&month=Jul&page_no=1&sort_by=asc&size=50&locale=en` |
| Period covered | July 2026 (FY 2026-27) |
| Retrieved | 2026-08-23 |
| NPCI record updated_at | 2026-08-19T12:48:56.665Z |
| Rows | 50 remitter banks × 4 categories = 200 data rows |
| Official XLSX | `https://www.npci.org.in/uploads/remitter_exec_July_26_460c043044.xlsx` |

### Volume-weighted aggregate (all 50 banks)

| Metric | Value |
|---|---:|
| Total volume | 2,481.395141 million executions |
| Weighted Approved % | 22.9527% |
| Weighted BD % | 76.1478% |
| Weighted TD % | 0.8969% |
| Total declined | 77.0447% |
| BD share of declined | 98.8359% |
| TD share of declined | 1.1641% |

## Snapshot 2: Payer-PSP Mandate Execution

**File:** `npci-autopay-payer-psp-execution-2026-07.csv`

| Field | Value |
|---|---|
| Source API | Same endpoint, `tab_name=psp-wise-execution&type_name=payer` |
| Period covered | July 2026 |
| Retrieved | 2026-08-23 |
| NPCI record updated_at | 2026-08-20T05:06:39.467Z |
| Rows | 18 payer PSPs × 4 categories = 72 data rows |
| Official XLSX | `https://www.npci.org.in/uploads/psp_exec_payer_July_26_698d9879ea.xlsx` |

### Volume-weighted aggregate (18 PSPs)

| Metric | Value |
|---|---:|
| Total volume | 610.109687 million executions |
| Weighted Approved % | 97.4828% |
| Weighted BD % | 2.4788% |
| Weighted TD % | 0.0385% |

## Bank Name Normalization & Mapping

Razorpay test-mode returns standard bank labels such as `"HDFC Bank"`, `"ICICI Bank"`, `"State Bank of India"`, `"Axis Bank"`, `"Canara Bank"`.
The NPCI AutoPay remitter snapshot uses official NPCI remitter names (e.g. `"HDFC BANK LTD"`, `"ICICI BANK LIMITED"`, `"STATE BANK OF INDIA"`, `"CANARA BANK"`).

The system normalizes both strings via case-insensitive fuzzy substring matching. For example, `"HDFC Bank"` matches `"HDFC BANK LTD"`, mapping the exact published NPCI approval (48.2%), BD (50.8%), and TD (1.0%) rates to the test-mode bank. This prevents mapping bias during batch generation and evaluation.

## Critical Interpretation Guardrail

The two tables measure **different layers of the payment flow**:

| Table | What it measures | Typical approval range |
|---|---|---|
| **Remitter bank execution** | Issuer-side debit outcome — whether the customer's bank approved or declined the mandate debit | 5–48% |
| **Payer PSP execution** | PSP processing performance — whether the payment app successfully forwarded the request to the issuer | 96–99% |

They must **never be combined into a single failure distribution**.
The simulator uses the **remitter-bank table** for failure-category calibration,
because that is where BD/TD decomposition lives.

## Why Not Other Sources?

| Source | Status | Reason |
|---|---|---|
| Dataful (dataful.in/datasets/18240) | Excluded | Paid subscription required for full download; free preview only shows partial rows |
| data.gov.in UPI/NPCI keywords | No results | Keyword pages currently return zero datasets |
| RBI Payment System Indicators (`PSIUserView.aspx?Id=53`) | Context only | Aggregate UPI/NACH volumes, no AutoPay-specific BD/TD breakdown |
| RBI DBIE (data.rbi.org.in/DBIE) | Context only | Monthly aggregate volumes, not AutoPay-specific |
| RBI Bulletin Table 45 | Context only | Aggregate, not AutoPay-specific |
| Old NPCI `/statistics/bd-td-and-uptime` | Dead link | Returns HTTP 404 as of Aug 2026 |
| AMFI SIP data | Cross-check only | Covers all SIP rails (NACH + cards + UPI), not UPI-only; used for terminal-state rate anchor (~4.7%/month discontinuation) |

## Regulatory Constraint Parameters

These parameters are extracted from official regulatory documents and constrain
the simulator's action space:

### Retry limit
- **Source:** NPCI Circular OC-215A/2025-26 (dated 21 May 2025, effective 1 Aug 2025)
- **URL:** https://www.npci.org.in/PDF/npci/upi/circular/2025/UPI-OC-No-215-A-FY-2025-26-Guidelines-on-usage-of-UPI-APIs.pdf
- **Rule:** Maximum 1 attempt + 3 retries per mandate per cycle

### Execution windows
- **Source:** Same circular OC-215A
- **Peak hours (prohibited):** 10:00–13:00 and 17:00–21:30 IST
- **Non-peak hours (permitted):** all other hours

### Pre-debit notification
- **Source:** RBI Digital Payments E-Mandate Framework 2026 (RBI/DPSS/2026-27/396, dated 21 Apr 2026)
- **URL:** https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=13374
- **Rule:** ≥24-hour advance notification before each debit

### Transaction limits (AFA-free)
- **Standard:** ₹15,000 per transaction
- **Enhanced categories:** ₹1,00,000 (insurance premiums, mutual fund subscriptions, credit-card bill payments)

### Deemed-debit response codes
- **Source:** NPCI OC-128A/2026-27 (dated 3 Jul 2026, effective 15 Jul 2026)
- **URL:** https://www.npci.org.in/uploads/UPI_OC_128_A_Addendum_to_OC_128_Extension_of_additional_response_codes_under_Deemed_Debit_for_mandate_execution_3b1e59eab2.pdf
- **Included in deemed-debit scope:** code 59 (suspected fraud), K1
- **Excluded from deemed-debit:** VO (court order), VH (tampered mandate), VU/QD (expired mandate), VS (duplicate mandate)

## Simulator Usage Contract

The generator reads the frozen CSV file at build time. It does not fetch live data.
This ensures reproducibility and prevents failures if the source site is slow or changes layout.

To refresh the snapshot:

1. Open https://www.npci.org.in/product/ecosystem-statistics/autopay in a browser
2. Use the browser's developer tools to capture the API response for both tables
3. Convert to the same CSV format used here
4. Update the filename with the new month
5. Update this README with the new retrieval date and values

## License

NPCI publishes these statistics publicly for informational purposes.
Data is reproduced verbatim without modification beyond CSV normalisation
(fiscal_year derived from year column, category labels preserved).
