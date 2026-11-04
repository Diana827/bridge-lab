Decide what to test next under a limited experimental budget, without post-hoc justification.

Dataset: NCI-ALMANAC combination screening data, train split only. Each train item is one ordered (drug1, drug2, cell line) group with an observed score: the unrounded mean over its dose points of (EXPECTEDGROWTH − PERCENTGROWTH). A HIT is an observed score greater than 0. Borderline items (|score| ≤ 1) are reported separately but count as hits under the primary rule.

Task: choose a selection method that, applied to train data only, ranks hidden candidate items for the next screening round. The primary evaluation is precision at a budget of 6,251 selections (10% of 62,510 hidden candidates), intention-to-treat: a failed or empty selection scores 0 hits. The same method is also evaluated at budgets 3,125 (5%) and 12,502 (20%).

Constraints: use only train outcomes. Hidden candidate identities are available; hidden outcomes are sealed and must not be assumed, estimated, or used. Chemistry proposals are untestable when molecular structure data is absent; this dataset has none. Do not give treatment, diagnosis, or dosing advice. Numbers in the final report must come from executed results, never from model text.
