# Evidence classifier evaluation

> **Synthetic authored examples only. This evaluation is not real-world or clinical validation.**

Dataset: 10 hand-written examples; class counts: `{'NOT_PREDICTED_MISSING': 5, 'PREDICTED_MISSING': 5}`. Stratified 5-fold cross-validation, shuffle enabled, seed `20261024`.
The dataset is tiny and each example is a template-like scenario. Fold results measure consistency on these authored examples only; they do not estimate generalization to real documents.

TF-IDF vectorization and Logistic Regression are fit inside each training fold using a scikit-learn Pipeline. The keyword baseline reads only the required-evidence name, document category, and authored document text; it has no learned preprocessing.

| Method | Accuracy | Balanced accuracy | Macro-F1 | Recall: not predicted missing | Recall: predicted missing | Confusion matrix | Fold balanced accuracy mean ± SD |
|---|---:|---:|---:|---:|---:|---|---:|
| majority baseline | 0.500000 | 0.500000 | 0.333333 | 1.000000 | 0.000000 | `[[5, 0], [5, 0]]` | 0.500000 ± 0.000000 |
| existing TF-IDF + Logistic Regression | 0.200000 | 0.200000 | 0.166667 | 0.000000 | 0.400000 | `[[0, 5], [3, 2]]` | 0.200000 ± 0.244949 |
| class-weighted Logistic Regression | 0.200000 | 0.200000 | 0.166667 | 0.000000 | 0.400000 | `[[0, 5], [3, 2]]` | 0.200000 ± 0.244949 |
| keyword/regex baseline | 1.000000 | 1.000000 | 1.000000 | 1.000000 | 1.000000 | `[[5, 0], [0, 5]]` | 1.000000 ± 0.000000 |

## Operational interpretation

Existing classifier macro-F1 minus the better of the majority and keyword baselines: `-0.833333` on this small synthetic set.
The classifier remains **Experimental — not used for decisions** regardless of this result. It cannot mark evidence present, missing, sufficient, verified, or satisfy policy criteria. Evidence status continues to come from deterministic review findings and authorized human verification.

## Limitations

- All 10 examples are authored synthetic examples; no labeled real documents or clinical outcomes are present.
- A five-fold split leaves two examples per fold; the metrics are extremely uncertain and sensitive to individual examples.
- The examples are not independent draws from a deployment population, so cross-validation does not establish external validity.
- The rule baseline is a simple token overlap heuristic, not a validated operational standard.
- Reviewer feedback is stored separately and is not automatically mixed into this evaluation or used for online retraining.

Reproduce with: `python backend/evaluate_evidence_classifier.py`.
