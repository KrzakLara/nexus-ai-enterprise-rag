# Ground-truth review checklist (human spot check)

**Status: NOT YET PERFORMED.** This checklist is for the repository owner (or another person). Until someone has actually completed it, the Phase 3 labels count as *AI-checked* (spot check of 12 documents, `eval/docintel/ERRATA.md`), **not** as independently human-verified.

**How:** open each source file, find each value yourself and compare it with the label. Do not trust the extractor's output, and do not use the UI.

- Labels file: `eval/docintel/labels.jsonl` (dataset v1.1).
- Canonical formats: dates `YYYY-MM-DD`; money as plain decimals (`8031.25`); durations ISO 8601 (`P90D` = 90 days, `P3M` = 3 months); `governing_law` is the short English name (*Republike Hrvatske* → `Croatia`); `value_period` is annual / total / monthly, as stated by the value's label; `null` means not stated in the document.
- Scanned PDFs have no text layer: read them visually.

Tick **OK**, or write what the document actually says. Report a mismatch as a new erratum; never edit the labels silently.

## 1. `di-c01`: documents/di-c01.pdf

multi-page English supply contract (text PDF): dates in words, annual value, auto-renewal on page 2. Split: dev; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| contract_id | `NS-SUP-2026-101` | 1 | ☐ |
| contract_type | `supply` | 1 | ☐ |
| effective_date | `2026-01-15` | 1 | ☐ |
| expiration_date | `2027-01-14` | 1 | ☐ |
| contract_value | `184000.00` | 1 | ☐ |
| value_period | `annual` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| payment_terms | `P45D` | 1 | ☐ |
| notice_period | `P90D` | 1 | ☐ |
| auto_renewal | `true` | 2 | ☐ |
| governing_law | `Croatia` | 2 | ☐ |
| parties | Northstar Manufacturing d.o.o.; Atlas Logistics GmbH | 1 | ☐ |

## 2. `di-c02`: documents/di-c02.pdf

Croatian maintenance contract (text PDF): Croatian long dates (`1. ožujka 2026.`), decimal comma, *no* auto-renewal. Split: dev; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| contract_id | `NS-ODR-2026-014` | 1 | ☐ |
| contract_type | `services` | 1 | ☐ |
| effective_date | `2026-03-01` | 1 | ☐ |
| expiration_date | `2027-02-28` | 1 | ☐ |
| contract_value | `48000.00` | 1 | ☐ |
| value_period | `total` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| payment_terms | `P30D` | 1 | ☐ |
| notice_period | `P60D` | 1 | ☐ |
| auto_renewal | `false` | 2 | ☐ |
| governing_law | `Croatia` | 2 | ☐ |
| parties | Northstar Manufacturing d.o.o.; Servis Plus d.o.o. | 1 | ☐ |

## 3. `di-c07`: documents/di-c07.pdf

**scanned** Croatian services contract: OCR source; diacritics in the party name. Split: dev; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| contract_id | `NS-USL-2026-045` | 1 | ☐ |
| contract_type | `services` | 1 | ☐ |
| effective_date | `2026-02-01` | 1 | ☐ |
| expiration_date | `2027-01-31` | 1 | ☐ |
| contract_value | `21600.00` | 1 | ☐ |
| value_period | `total` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| payment_terms | `P30D` | 1 | ☐ |
| notice_period | `P30D` | 1 | ☐ |
| auto_renewal | `true` | 1 | ☐ |
| governing_law | `Croatia` | 1 | ☐ |
| parties | Northstar Manufacturing d.o.o.; Čisto i Sjajno d.o.o. | 1 | ☐ |

## 4. `di-c04`: documents/di-c04.txt

NDA (TXT): **missing** value, payment terms, notice period and renewal clause; day-first dates `10/02/2026`. Split: dev; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| contract_id | `NS-NDA-2026-007` | 1 | ☐ |
| contract_type | `nda` | 1 | ☐ |
| effective_date | `2026-02-10` | 1 | ☐ |
| expiration_date | `2029-02-09` | 1 | ☐ |
| contract_value | `null` | – | ☐ |
| value_period | `null` | – | ☐ |
| currency | `null` | – | ☐ |
| payment_terms | `null` | – | ☐ |
| notice_period | `null` | – | ☐ |
| auto_renewal | `null` | – | ☐ |
| governing_law | `England and Wales` | 2 | ☐ |
| parties | Northstar Manufacturing d.o.o.; Helios Analytics Ltd | 1 | ☐ |

## 5. `di-c13`: documents/di-c13.txt

indefinite lease (TXT): **no** expiration date; value is a **monthly** rent (erratum C). Split: test; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| contract_id | `NS-NAJ-2026-002` | 1 | ☐ |
| contract_type | `lease` | 1 | ☐ |
| effective_date | `2026-04-01` | 1 | ☐ |
| expiration_date | `null` | – | ☐ |
| contract_value | `3600.00` | 1 | ☐ |
| value_period | `monthly` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| payment_terms | `P10D` | 1 | ☐ |
| notice_period | `P3M` | 1 | ☐ |
| auto_renewal | `null` | – | ☐ |
| governing_law | `Croatia` | 2 | ☐ |
| parties | Northstar Manufacturing d.o.o.; Gradski Centar Nekretnine d.o.o. | 1 | ☐ |

## 6. `di-i01`: documents/di-i01.pdf

English invoice (text PDF): line items, subtotal / VAT / total arithmetic. Split: dev; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| invoice_number | `INV-2026-0412` | 1 | ☐ |
| supplier_name | `Atlas Logistics GmbH` | 1 | ☐ |
| supplier_tax_id | `DE812345671` | 1 | ☐ |
| customer_name | `Northstar Manufacturing d.o.o.` | 1 | ☐ |
| invoice_date | `2026-03-31` | 1 | ☐ |
| due_date | `2026-04-30` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| subtotal | `6425.00` | 1 | ☐ |
| tax_amount | `1606.25` | 1 | ☐ |
| total_amount | `8031.25` | 1 | ☐ |
| payment_reference | `NS-PO-7781` | 1 | ☐ |
| iban | `DE89370400440532013000` | 1 | ☐ |
| line 1 | Freight Zagreb-Rijeka · qty 12 · price 450.00 · VAT 25 % · amount 5400.00 | 1 | ☐ |
| line 2 | Warehouse handling · qty 40 · price 18.50 · VAT 25 % · amount 740.00 | 1 | ☐ |
| line 3 | Customs documentation · qty 3 · price 95.00 · VAT 25 % · amount 285.00 | 1 | ☐ |

## 7. `di-i02`: documents/di-i02.pdf

Croatian invoice (text PDF): **two VAT rates** (25 % and 5 %), decimal comma. Split: dev; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| invoice_number | `R-2026-0153` | 1 | ☐ |
| supplier_name | `Servis Plus d.o.o.` | 1 | ☐ |
| supplier_tax_id | `HR12345678903` | 1 | ☐ |
| customer_name | `Northstar Manufacturing d.o.o.` | 1 | ☐ |
| invoice_date | `2026-04-15` | 1 | ☐ |
| due_date | `2026-05-15` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| subtotal | `1207.00` | 1 | ☐ |
| tax_amount | `276.55` | 1 | ☐ |
| total_amount | `1483.55` | 1 | ☐ |
| payment_reference | `HR00 2026-153` | 1 | ☐ |
| iban | `HR1210010051863000160` | 1 | ☐ |
| line 1 | Servis kompresora · qty 2 · price 380.00 · VAT 25 % · amount 760.00 | 1 | ☐ |
| line 2 | Rezervni dijelovi · qty 5 · price 64.20 · VAT 25 % · amount 321.00 | 1 | ☐ |
| line 3 | Stručna literatura · qty 3 · price 42.00 · VAT 5 % · amount 126.00 | 1 | ☐ |

## 8. `di-i04`: documents/di-i04.xlsx

Croatian invoice (**XLSX**): printed subtotal is **wrong** on purpose (expected error `subtotal_mismatch`). Split: dev; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| invoice_number | `R-2026-0377` | 1 | ☐ |
| supplier_name | `Datanova d.o.o.` | 1 | ☐ |
| supplier_tax_id | `HR98765432106` | 1 | ☐ |
| customer_name | `Northstar Manufacturing d.o.o.` | 1 | ☐ |
| invoice_date | `2026-05-20` | 1 | ☐ |
| due_date | `2026-06-19` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| subtotal | `2180.00` | 1 | ☐ |
| tax_amount | `520.00` | 1 | ☐ |
| total_amount | `2700.00` | 1 | ☐ |
| payment_reference | `HR00 2026-377` | 1 | ☐ |
| iban | `HR1723600001101234565` | 1 | ☐ |
| line 1 | Licenca Datanova ERP · qty 10 · price 120.00 · VAT 25 % · amount 1200.00 | 1 | ☐ |
| line 2 | Implementacija · qty 16 · price 55.00 · VAT 25 % · amount 880.00 | 1 | ☐ |
| expected validation result | subtotal_mismatch | – | ☐ (recompute it yourself) |

## 9. `di-i07`: documents/di-i07.pdf

**scanned** Croatian invoice: printed total is **wrong** on purpose (`total_mismatch`); IBAN read by OCR. Split: dev; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| invoice_number | `R-2026-0911` | 1 | ☐ |
| supplier_name | `Čisto i Sjajno d.o.o.` | 1 | ☐ |
| supplier_tax_id | `HR33445566770` | 1 | ☐ |
| customer_name | `Northstar Manufacturing d.o.o.` | 1 | ☐ |
| invoice_date | `2026-08-31` | 1 | ☐ |
| due_date | `2026-09-30` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| subtotal | `1886.40` | 1 | ☐ |
| tax_amount | `471.60` | 1 | ☐ |
| total_amount | `2408.00` | 1 | ☐ |
| payment_reference | `HR00 2026-911` | 1 | ☐ |
| iban | `HR0923400091110001234` | 1 | ☐ |
| line 1 | Čišćenje hale · qty 4 · price 450.00 · VAT 25 % · amount 1800.00 | 1 | ☐ |
| line 2 | Sredstva za čišćenje · qty 1 · price 86.40 · VAT 25 % · amount 86.40 | 1 | ☐ |
| expected validation result | total_mismatch | – | ☐ (recompute it yourself) |

## 10. `di-i11`: documents/di-i11.md

Croatian invoice (Markdown): **no IBAN** printed (missing field). Split: test; tenant: northstar.

| Field | Label | Page | OK? / document says |
|---|---|---|---|
| invoice_number | `R-2026-1020` | 1 | ☐ |
| supplier_name | `Panonija Metal d.d.` | 1 | ☐ |
| supplier_tax_id | `HR55667788992` | 1 | ☐ |
| customer_name | `Northstar Manufacturing d.o.o.` | 1 | ☐ |
| invoice_date | `2026-11-03` | 1 | ☐ |
| due_date | `2026-12-03` | 1 | ☐ |
| currency | `EUR` | 1 | ☐ |
| subtotal | `5970.00` | 1 | ☐ |
| tax_amount | `1492.50` | 1 | ☐ |
| total_amount | `7462.50` | 1 | ☐ |
| payment_reference | `HR00 2026-1020` | 1 | ☐ |
| iban | `null` | – | ☐ |
| line 1 | Čelični limovi 2 mm · qty 150 · price 38.40 · VAT 25 % · amount 5760.00 | 1 | ☐ |
| line 2 | Prijevoz · qty 1 · price 210.00 · VAT 25 % · amount 210.00 | 1 | ☐ |

## Sign-off

- Reviewer: ______________________   Date: ____________
- Documents checked: __ / 10 · values checked: ____ · mismatches found: ____ (list them as errata)
- Only after this sign-off may the documentation describe the labels as *human-verified* (for these 10 documents).
