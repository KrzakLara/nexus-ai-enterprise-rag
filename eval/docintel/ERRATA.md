# Phase 3 corpus errata: dataset v1.0 → v1.1 (2026-11-14)

Three ground-truth issues were found in a manual spot check of 12 documents (about 140 values) against their source files. The check was done by the AI assistant, not by an independent human annotator. All three are **label** problems: **none of the 32 source documents changed** (SHA-256 identical, verified), and no amount, date, party, split, ACL group or expected validation error changed.

| | v1.0 | v1.1 |
|---|---|---|
| `manifest.json` SHA-256 | `5ada74e1fbf883e5f7aab01ea58a21cb4a29e778c56b0e470ac45f7d2b1fca65` | `aa0a85216914635bb885f223b8c832168c2cb363b408fd272a9c21bf444b81e3` |
| `labels.jsonl` SHA-256 | `8ce90ac2e89aeec290580e172b7c1481acc6150c3bd3da409e2cd7d18e11d3c7` | `c3c53289747701b399f434a6b1ad53bda150932d493114c586fe66efd21b69fa` |
| documents (32 files) | unchanged | unchanged |

The v1.0 labels, manifest, evaluation report (`latest.json`, SHA-256 `c05a4a89…0380b`) and rendered results are preserved in `reports/archive/dataset-v1.0-*`. Metric differences are listed in `reports/archive/ERRATA-impact.md`.

## A. Numbers in exponent notation (8 values, format only)

- **Problem:** the generator wrote line-item quantities and tax rates with `str(Decimal.normalize())`, which renders 40 as `4E+1`. Affected values: di-i01 qty `4E+1`, di-i04 qty `1E+1`, di-i05 tax rate `2E+1` (twice), di-i06 qty `5E+2` and `2E+1`, di-i10 qty `4E+1`, di-i11 qty `1.5E+2`. The spot check first counted 6; a full scan found 8.
- **Impact in v1.0:**
  - Metrics: none, since the evaluation compares numbers as Decimals.
  - The values violated the canonical label format.
  - Latent bug: the scripted reviewer sends ground-truth values as corrections, and the review API parsed `4E+1` as the amount `4`.
- **Fix:**
  - Canonical decimal strings (`format(Decimal.normalize(), "f")` → `40`, `150`, `20`).
  - The review API now reads scientific notation as a number (`normalize_correction`), with a regression test.
  - A dataset test rejects exponent notation in any numeric label.

## B. Two names for the same jurisdiction (8 values)

- **Problem:** English contracts were labelled `Republic of Croatia` and Croatian contracts `Croatia` for the same jurisdiction. A comparison would have reported a false difference between equivalent governing laws.
- **Rule (documented in `rules.py` and `docs/document-intelligence.md`):** `governing_law` is the **short English name of the jurisdiction** (`Croatia`, `Ireland`, `England and Wales`). The equivalent wordings `Republic of Croatia`, `the Republic of Croatia`, `Republike Hrvatske` and `Hrvatske` map to `Croatia`. The document's own wording stays in `raw` and in the evidence, and grounding accepts the mapped value only through the same alias table.
- **Affected labels:** di-c01, c06, c08, c09, c10, c12, c14, c15 (`Republic of Croatia` → `Croatia`).

## C. `contract_value` had no defined meaning (new field `value_period`, 15 values)

- **Investigation:** the source documents state three different quantities under the one field `contract_value`:

  | Printed label | Quantity | Documents |
  |---|---|---|
  | *Annual value* | an annual amount | c01, c03, c06, c08, c09, c10, c14, c15 |
  | *Vrijednost ugovora* / *Contract value* | the total contract value | c02, c05, c07, c11, c12, c16 |
  | *Mjesečna najamnina* ("monthly rent") | a **monthly** amount | c13 |

  di-c13 (`documents/di-c13.txt`) is an indefinite office lease with no expiration date. It states `Mjesečna najamnina: 3.600,00 EUR`. The monthly amount is **explicit, not ambiguous**. What the document does not state, and what nobody could derive without inventing a duration, is a total contract value.
- **Decision:** `contract_value` = the principal monetary amount the contract states. The new field **`value_period` ∈ {annual, total, monthly}** records what that amount covers, as stated by its label.
  - c13 keeps `contract_value = 3600.00` with `value_period = monthly`. No total is invented, and the label is not marked ambiguous, because the source is not.
  - Contracts without a value (c04) have neither field.
- **System changes:**
  - the rules provider derives `value_period` from the matched value label;
  - grounding checks the period against the label and period keywords;
  - validation warns when a value has no period and rejects other period values;
  - **comparison no longer ranks amounts with different or unknown periods** (`comparable: false`);
  - the review API accepts only the three periods.
- **Not changed on purpose:** the rules still do not know c13's label *Mjesečna najamnina*. c13 is a held-out test document with deliberately novel labels, so the miss stays visible in the metrics.

## Reproduce

```bash
python scripts/build_docintel_dataset.py --check     # v1.1 files unchanged
git diff <v1.0 commit> -- eval/docintel/documents   # empty: no source document changed
```
