# DATA.md — BRIDGE LAB, step 1 only

This document describes the first dataset prepared for BRIDGE LAB.
It is a dataset-provenance document. It does not contain the dataset
itself, it does not contain a sealed slice, and it makes no clinical
claim. No experiment, split, seal, ranking, or evaluation has been
run on this dataset in this step.

## Project claim, restated

BRIDGE LAB is a research-method validation system. It does not claim
scientific truth, clinical utility, or treatment benefit. All results
in this project are from an in-vitro cell-line screen, and no
patient-level conclusion may be drawn from them.

## Source — official NCI only

- **Dataset:** NCI-ALMANAC, Growth Inhibition Data
- **Official sources checked 2026-10-27:**
  - https://wiki.nci.nih.gov/spaces/NCIDTPdata/pages/338237347/NCI-ALMANAC
  - https://discover.nci.nih.gov/cellminer/loadDownload.do
  - https://www.discover.nci.nih.gov/cellminer/html/drug_almanac_combo_score.html
- **Primary publication:** Holbeck et al., *Cancer Research*
  2017;77(13):3564–3576, DOI 10.1158/0008-5472.CAN-17-0489.
  The combination-benefit method in that paper, ComboScore, is
  described as a modification of Bliss independence.
- **Exact download URL (as shown on the NCI Wiki page):**
  https://wiki.nci.nih.gov/download/attachments/338237347/ComboDrugGrowth_Nov2017.zip?version=1&modificationDate=1510057275000&api=v2
- **File name:** `ComboDrugGrowth_Nov2017.zip`
- **Download date:** 2026-10-27
- **Wiki attachment size (as shown on the Wiki page):** 81.98 MB
- **Wiki attachment created:** 2017-11-07 07:21 (as shown on the Wiki
  page); Wiki page last updated 2017-12-22 (as shown on the Wiki page)
- **Licence / usage terms:** unavailable — no licence or usage
  terms were shown on the Wiki body page or attachment-list page
  checked for this audit. No licence is assumed here. Check the
  licence before any redistribution, and record it here before
  redistribution.
- **Other files:** none downloaded in this step. In particular, no
  molecular structure file was downloaded, as instructed.

## Patient data — none

The 29 columns in the file (listed below) contain drug identifiers,
study/plate identifiers, panel and cell-line identifiers, concentrations,
and measured growth values. No column contains a patient identifier,
patient name, date of birth, or other patient-level private data.
This dataset is a cell-line screen, not patient-level data.
That statement is about the columns in this file; it is not a claim
about any other NCI dataset.

## Raw archive — unmodified

- **Location:** `data/raw/ComboDrugGrowth_Nov2017.zip`
- **Size on disk:** 85,966,226 bytes
- **SHA-256:**
  `69367a8b654908ebf70a752548c8d240fda2b751336eff0241a5986628696100`
- **Archive type:** Zip archive, deflate
- **Contents (from `unzip -l`, not from extraction):**
  - `ComboDrugGrowth_Nov2017.csv` — 615,194,859 bytes uncompressed,
    dated 2017-11-01 08:47 in the archive
  - No other member. Exactly 1 file.
- **Rule:** the raw archive is never modified. All work reads it
  read-only. Any future cleaning is a new file under
  `data/processed/`, with its own recorded transformation.

## Fields — as supplied, 29 columns

Header, in file order:

`COMBODRUGSEQ, SCREENER, STUDY, TESTDATE, PLATE, PANELNBR, CELLNBR,
PREFIX1, NSC1, SAMPLE1, CONCINDEX1, CONC1, CONCUNIT1, PREFIX2, NSC2,
SAMPLE2, CONCINDEX2, CONC2, CONCUNIT2, PERCENTGROWTH,
PERCENTGROWTHNOTZ, TESTVALUE, CONTROLVALUE, TZVALUE,
EXPECTEDGROWTH, SCORE, VALID, PANEL, CELLNAME`

Field notes — only what was verified in this audit:

- `NSC1`, `NSC2` are drug identifiers as supplied. No drug-name
  mapping is included in this file, and none was invented here.
  A future mapping, if added, must be a separate, sourced step.
- `PANEL` and `CELLNAME` are the panel and cell-line identifiers
  as supplied. Together they are the condition identifier for
  the canonical model used in this project
  (`drug_a, drug_b, condition_or_cell_line, observed_score` in
  VALIDATION_PLAN.md).
- `CONCUNIT1` / `CONCUNIT2` have exactly 1 distinct non-missing
  value each in the audit; the value itself is not recorded here
  because the audit counted distinct values without recording a
  unit claim. Check the value in the processed field summary
  before quoting a unit.
- `SCORE` is a column as supplied. Its exact definition (whether
  it is the paper's ComboScore, and for which rows it is defined)
  was not verified from the file header alone, and is therefore
  **not** asserted here. Do not quote `SCORE` as ComboScore
  until its definition is verified against NCI documentation.
- `EXPECTEDGROWTH` is a column as supplied; its exact definition
  is likewise not asserted here.
- `VALID` has exactly 1 distinct non-missing value in the audit:
  `Y`, for all rows. That is a counted fact, not a claim that
  every row passed an external quality check.

## Validation audit — counted, not invented

Audit method: streaming read of the CSV inside the zip, via
`scripts/prepare_almanac.py` (stdlib only). Missing is defined as
an empty string after stripping. No value was imputed, no row was
removed, no duplicate was dropped in this step.

- **Row count:** 3,686,475
- **Duplicate `COMBODRUGSEQ` count:** 0
- **Duplicate full-row count:** 0
- **Distinct counts (selected, full table in processed output):**
  - `CELLNAME`: 61 distinct non-missing values
  - `PANEL`: 9
  - `NSC1`: 105, `NSC2`: 104
  - `CELLNBR`: 24, `PANELNBR`: 9
  - `SCORE`: 414 distinct non-missing values
- **Missing counts (empty after stripping):**
  - 0 missing: `COMBODRUGSEQ, SCREENER, STUDY, TESTDATE, PLATE,
    PANELNBR, CELLNBR, PREFIX1, NSC1, SAMPLE1, CONCINDEX1, CONC1,
    CONCUNIT1, CONCINDEX2, PERCENTGROWTH, TESTVALUE, CONTROLVALUE,
    TZVALUE, VALID, PANEL, CELLNAME`
  - `NSC2`: 812,961 missing
  - `SAMPLE2`: 812,961 missing
  - `CONC2`: 812,961 missing
  - `CONCUNIT2`: 69,678 missing
  - `PREFIX2`: 105,588 missing
  - `PERCENTGROWTHNOTZ`: 208,605 missing
  - `EXPECTEDGROWTH`: 815,031 missing
  - `SCORE`: 815,031 missing
- **Interpretation — not yet verified:** the equal missing counts
  for `NSC2`, `SAMPLE2`, and `CONC2` (812,961 each) are consistent
  with single-agent rows, and the equal missing counts for
  `EXPECTEDGROWTH` and `SCORE` (815,031 each) are consistent with
  rows for which no combination score is defined. Whether those
  are exactly the same rows was **not** verified in this audit,
  and is not claimed here. Verify row-level coincidence before
  any experiment that filters on these fields.

## Processed outputs — only under data/processed/

Script: `scripts/prepare_almanac.py`. It reads the raw zip only
and writes only under `data/processed/`.

- `data/processed/field_summary.csv` — per column: row count,
  missing count, non-missing count, distinct non-missing count.
- `data/processed/pair_cell_counts.csv` — row counts per
  (`NSC1`, `NSC2`, `CELLNAME`, `VALID`). Row counts only; no
  score aggregation, no ranking, no selection.
- `data/processed/validation_report.json` — the full audit above,
  including the raw archive SHA-256, header, missing counts,
  distinct counts, duplicate counts, and the transformation list.

Transformations performed in this step, exactly:

1. Downloaded only `ComboDrugGrowth_Nov2017.zip` from the exact
   URL above, to `data/raw/`, unmodified.
2. Computed its SHA-256 and listed its zip contents.
3. Streamed the inner CSV once, counting rows, missing values,
   distinct values, duplicate `COMBODRUGSEQ` values, and
   duplicate full rows.
4. Wrote the three processed files above. No row-level cleaned
   CSV was created, no value was imputed, no drug name was added,
   no structure file was used.

## Computation layer

The Python computation layer is in `bridgelab/compute.py`, with
registered tool wrappers in `bridgelab/tools.py`. It is the only
place where numbers are computed in this project. Muse never
computes metrics. See `tests/test_compute.py` for valid, invalid,
missing-value, and mismatched-array tests, and
`tests/test_data_almanac.py` for raw-archive validation tests.
No experiment, split, seal, or evaluation on the real ALMANAC
data has been run in this step; computation-layer tests use
synthetic data clearly labelled as synthetic, plus raw-archive
metadata checks that do not load the full CSV.

## Open items — do not invent

- Licence / usage terms for the Wiki attachment: unavailable.
- Exact definition of `SCORE` and `EXPECTEDGROWTH`: not verified
  from the file header alone.
- Concentration unit value for `CONCUNIT1` / `CONCUNIT2`:
  check the processed field summary before quoting.
- Row-level coincidence of the missing-value patterns above:
  not verified.
- Drug-name mapping for `NSC1` / `NSC2`: not in this file,
  not added here.
- Molecular structures: not downloaded, not used; chemistry
  proposals for this dataset are therefore untestable until a
  structure source is added in a separate, recorded step.
