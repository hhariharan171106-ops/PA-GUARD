"""Evaluate PA Guard's tiny authored synthetic evidence set with stratified CV."""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, f1_score, recall_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline

from evidence_prediction_model import _SYNTHETIC_EXAMPLES, _feature_text

SEED = 20261024
LABELS = ["NOT_PREDICTED_MISSING", "PREDICTED_MISSING"]
OUTPUT = Path(__file__).parent.parent / "docs" / "EVIDENCE_EVALUATION.md"

def keyword_prediction(example) -> str:
    required, category, text, _ = example
    available = f"{category} {text}".casefold()
    tokens = [token for token in re.findall(r"[a-z0-9]+", required.casefold()) if len(token) > 2]
    return "NOT_PREDICTED_MISSING" if any(re.search(rf"\b{re.escape(token)}\b", available) for token in tokens) else "PREDICTED_MISSING"

def summarize(y_true, y_pred):
    recalls = recall_score(y_true, y_pred, labels=LABELS, average=None, zero_division=0)
    return {"accuracy":float(accuracy_score(y_true,y_pred)),
        "balanced_accuracy":float(balanced_accuracy_score(y_true,y_pred)),
        "macro_f1":float(f1_score(y_true,y_pred,labels=LABELS,average="macro",zero_division=0)),
        "per_class_recall":{label:float(value) for label,value in zip(LABELS,recalls)},
        "confusion_matrix_actual_label_order":confusion_matrix(y_true,y_pred,labels=LABELS).tolist()}

def main():
    examples=list(_SYNTHETIC_EXAMPLES)
    y=np.array([row[3] for row in examples])
    texts=[_feature_text(label,text,[category]) for label,category,text,_ in examples]
    counts={label:int(sum(value==label for value in y)) for label in LABELS}
    folds=min(5,min(counts.values()))
    cv=StratifiedKFold(n_splits=folds,shuffle=True,random_state=SEED)
    estimators={
        "majority baseline":Pipeline([("tfidf",TfidfVectorizer(ngram_range=(1,2),lowercase=True)),("classifier",DummyClassifier(strategy="most_frequent"))]),
        "existing TF-IDF + Logistic Regression":Pipeline([("tfidf",TfidfVectorizer(ngram_range=(1,2),lowercase=True)),("classifier",LogisticRegression(random_state=0,max_iter=1000))]),
        "class-weighted Logistic Regression":Pipeline([("tfidf",TfidfVectorizer(ngram_range=(1,2),lowercase=True)),("classifier",LogisticRegression(random_state=0,max_iter=1000,class_weight="balanced"))]),
    }
    results={}
    for name,model in estimators.items():
        oof=np.empty(len(y),dtype=object)
        fold_scores=[]
        for train_idx,test_idx in cv.split(texts,y):
            model.fit([texts[i] for i in train_idx],y[train_idx])
            prediction=model.predict([texts[i] for i in test_idx])
            oof[test_idx]=prediction
            fold_scores.append(summarize(y[test_idx],prediction))
        aggregate=summarize(y,oof.tolist())
        aggregate["fold_mean_std"]={key:{"mean":float(np.mean([s[key] for s in fold_scores])),"std":float(np.std([s[key] for s in fold_scores],ddof=0))} for key in ("accuracy","balanced_accuracy","macro_f1")}
        results[name]=aggregate
    keyword_pred=[keyword_prediction(example) for example in examples]
    keyword_scores=[summarize(y[test_idx],np.array(keyword_pred,dtype=object)[test_idx].tolist()) for _,test_idx in cv.split(texts,y)]
    keyword_aggregate=summarize(y,keyword_pred)
    keyword_aggregate["fold_mean_std"]={key:{"mean":float(np.mean([s[key] for s in keyword_scores])),"std":float(np.std([s[key] for s in keyword_scores]))} for key in ("accuracy","balanced_accuracy","macro_f1")}
    results["keyword/regex baseline"]=keyword_aggregate
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    lines=["# Evidence classifier evaluation","","> **Synthetic authored examples only. This evaluation is not real-world or clinical validation.**","",
      f"Dataset: {len(examples)} hand-written examples; class counts: `{counts}`. Stratified {folds}-fold cross-validation, shuffle enabled, seed `{SEED}`.",
      "The dataset is tiny and each example is a template-like scenario. Fold results measure consistency on these authored examples only; they do not estimate generalization to real documents.","",
      "TF-IDF vectorization and Logistic Regression are fit inside each training fold using a scikit-learn Pipeline. The keyword baseline reads only the required-evidence name, document category, and authored document text; it has no learned preprocessing.","",
      "| Method | Accuracy | Balanced accuracy | Macro-F1 | Recall: not predicted missing | Recall: predicted missing | Confusion matrix | Fold balanced accuracy mean ± SD |","|---|---:|---:|---:|---:|---:|---|---:|"]
    for name,result in results.items():
        f=result["fold_mean_std"]["balanced_accuracy"]
        lines.append(f"| {name} | {result['accuracy']:.6f} | {result['balanced_accuracy']:.6f} | {result['macro_f1']:.6f} | {result['per_class_recall'][LABELS[0]]:.6f} | {result['per_class_recall'][LABELS[1]]:.6f} | `{result['confusion_matrix_actual_label_order']}` | {f['mean']:.6f} ± {f['std']:.6f} |")
    best_baseline=max(results["majority baseline"]["macro_f1"],results["keyword/regex baseline"]["macro_f1"])
    model_score=results["existing TF-IDF + Logistic Regression"]["macro_f1"]
    lines += ["","## Operational interpretation","",
      f"Existing classifier macro-F1 minus the better of the majority and keyword baselines: `{model_score-best_baseline:+.6f}` on this small synthetic set.",
      "The classifier remains **Experimental — not used for decisions** regardless of this result. It cannot mark evidence present, missing, sufficient, verified, or satisfy policy criteria. Evidence status continues to come from deterministic review findings and authorized human verification.","",
      "## Limitations","",
      "- All 10 examples are authored synthetic examples; no labeled real documents or clinical outcomes are present.",
      "- A five-fold split leaves two examples per fold; the metrics are extremely uncertain and sensitive to individual examples.",
      "- The examples are not independent draws from a deployment population, so cross-validation does not establish external validity.",
      "- The rule baseline is a simple token overlap heuristic, not a validated operational standard.",
      "- Reviewer feedback is stored separately and is not automatically mixed into this evaluation or used for online retraining.","",
      "Reproduce with: `python backend/evaluate_evidence_classifier.py`.",""]
    OUTPUT.write_text("\n".join(lines),encoding="utf-8")
    print(f"dataset_size={len(examples)} class_counts={counts} folds={folds} seed={SEED}")
    for name,result in results.items(): print(f"{name}: accuracy={result['accuracy']:.6f} balanced_accuracy={result['balanced_accuracy']:.6f} macro_f1={result['macro_f1']:.6f} per_class_recall={result['per_class_recall']} confusion={result['confusion_matrix_actual_label_order']} folds_balanced_accuracy={result['fold_mean_std']['balanced_accuracy']}")
    print(f"Wrote {OUTPUT}")

if __name__ == "__main__": main()
