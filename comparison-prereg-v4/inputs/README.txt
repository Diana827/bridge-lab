Built on NCI-ALMANAC (public NCI Developmental Therapeutics Program data), retrieved 2026-10-27.
nsc_structure.csv: nsc, molfile (V2000 mol block), formula, cas, name. 104 compounds from ComboCompoundSet.sdf.
  There is NO smiles or inchi column: the SDF holds mol blocks only and no chemistry toolkit (RDKit) is installed here.
candidate_items.csv: item_id, nsc1, nsc2, cellname. 293626 drug-pair x cell-line items from the NCI-ALMANAC pair score table.
NSC 753082 appears in the items but has no structure in the SDF.

candidate_items.csv — provenance and known coverage gap:
  Source: built from the same NCI-ALMANAC download as the rest of this package
  (ComboDrugGrowth_Nov2017.zip, official NCI Wiki, retrieved 2026-10-27;
  Holbeck et al., Cancer Research 2017). Item ids only; this file contains
  no observed scores.
  Coverage against the sealed E0003 pair split: 293,626 items in total —
  234,040 on train pairs and 59,586 on hidden pairs, covering 1,021 of the
  1,049 hidden pairs. 28 hidden pairs (2,924 items) are absent from this
  file, so it does not cover the full hidden candidate pool.
  Note: candidate_items.csv was NOT the candidate list scored in E0003.
  E0003 used comparison-prereg-v4/hidden_candidates.csv (62,510 items).
  candidate_items.csv is an input prepared for the E0004 draft
  (comparison-prereg-v5/).
