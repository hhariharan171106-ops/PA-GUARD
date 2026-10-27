# Eka Medical Records Parsing Dataset Evaluation Report

## Experiment Summary
* **Dataset Source:** `ekacare/medical_records_parsing_validation_set`
* **Random Seed:** `42`
* **Total Samples:** 288 (288 images)
* **Unique MD5s:** 288 (Duplicates: 0)
* **Document Types:** {'Prescription': 70, 'Lab-report': 218}
* **Split Scheme:** Stratified 75% Train (216) / 25% Test (72)
* **Features Extracted:** 15 lightweight visual layout features (aspect ratio, line gradients, projection variance, 8-bin intensity histogram)
* **Execution Time:** 30.36s

## Performance Comparison

| Metric | Majority Baseline (Lab-report) | Lightweight Classifier (Balanced LogReg) |
|---|---|---|
| Accuracy | 0.7639 | 0.5278 |
| Macro Precision | 0.3819 | 0.4910 |
| Macro Recall | 0.5000 | 0.4877 |
| Macro F1-Score | 0.4331 | 0.4688 |
| Weighted F1-Score | 0.6616 | 0.5622 |

### Per-Class Performance
* **Lab-report:** Precision = 0.7561, Recall = 0.5636, F1 = 0.6458
* **Prescription:** Precision = 0.2258, Recall = 0.4118, F1 = 0.2917

### Confusion Matrix (Test Split: rows=Actual, cols=Predicted)
Classes: `[np.str_('Lab-report'), np.str_('Prescription')]`
```
[[31, 24], [10, 7]]
```

## Critical Domain Findings & Limitations
1. **Two-Class Restriction:** The Eka dataset only labels `Lab-report` and `Prescription`. Prior authorization requires diverse clinical documentation including Clinical Notes, Imaging Reports, Specialist Letters, and Treatment Histories.
2. **Format vs. Necessity:** Identifying that a document is a prescription or lab report establishes document type only; it **does not** establish whether payer prior-authorization clinical necessity criteria are satisfied.
3. **No Clinical Text Annotations:** The dataset provides prompt templates and evaluation rubrics for JSON parsing, not ground-truth patient clinical narratives.
4. **Safety & Compatibility:** The production TF-IDF evidence gap model (`evidence_prediction_model.py`) is preserved intact to prevent regression in the existing case review workflow.
