"""Reproducible synthetic-only SLA comparison with a shifted test simulator."""
from __future__ import annotations

import json
import math
import random
import hashlib
import argparse
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, average_precision_score, brier_score_loss,
    confusion_matrix, f1_score, precision_score, recall_score, roc_auc_score)
from sklearn.model_selection import train_test_split

from sla_risk_model import create_pipeline
from sla_risk_synthetic_data import generate_synthetic_dataset
from train_sla_risk_model import GENERATOR_ASSUMPTIONS

SEED = 20261024
SHIFTED_SEED = 20261025
N_TRAIN = 6000
N_SHIFTED = 3000
BACKEND_DIR = Path(__file__).parent
OUT = BACKEND_DIR.parent / "docs"
CARD_JSON = BACKEND_DIR / "model_card.json"
DOCS_CARD_JSON = OUT / "model_card.json"
CARD_MD = OUT / "MODEL_CARD.md"
DEFAULT_ARTIFACT = BACKEND_DIR / "models" / "sla_shifted_final_20261024.joblib"

# Separate scenario assumptions. Names and values are simulation choices, not payer facts.
SHIFT = {
    "payer_mix": {"Northstar Health": .10, "Harbor Mutual": .15, "SummitCare": .25,
                  "Cedar Valley Plan": .50},
    "service_mix": {"Lumbar MRI": .10, "Specialty infusion": .40,
                    "Knee arthroscopy": .15, "Cardiac CT": .25, "Physical therapy": .10},
    "status_mix": {"NEW": .05, "INTAKE REVIEW": .10, "EVIDENCE REVIEW": .15,
                   "READY TO SUBMIT": .05, "SUBMITTED": .15, "PAYER PENDING": .25,
                   "MORE INFORMATION REQUIRED": .25},
    "payer_delay": {"Northstar Health": 10., "Harbor Mutual": 15., "SummitCare": 24., "Cedar Valley Plan": 32.},
    "service_delay": {"Lumbar MRI": 3., "Specialty infusion": 22., "Knee arthroscopy": 10., "Cardiac CT": 15., "Physical therapy": 0.},
    "status_delay": {"NEW": 10., "INTAKE REVIEW": 15., "EVIDENCE REVIEW": 24., "READY TO SUBMIT": 8., "SUBMITTED": 30., "PAYER PENDING": 44., "MORE INFORMATION REQUIRED": 58.},
    "duration_noise_sigma": .85,
    "hours_range": [2., 300.],
}
TRAIN_EXPECTED = {
    "payer": {"Northstar Health": 8., "Harbor Mutual": 12., "SummitCare": 18., "Cedar Valley Plan": 24.},
    "service": {"Lumbar MRI": 0., "Specialty infusion": 16., "Knee arthroscopy": 8., "Cardiac CT": 12., "Physical therapy": -4.},
    "status": {"NEW": 8., "INTAKE REVIEW": 12., "EVIDENCE REVIEW": 18., "READY TO SUBMIT": 6., "SUBMITTED": 24., "PAYER PENDING": 36., "MORE INFORMATION REQUIRED": 48.},
}

def shifted_data(n: int = N_SHIFTED, seed: int = SHIFTED_SEED):
    r = random.Random(seed)
    rows, labels = [], []
    for _ in range(n):
        status = r.choices(list(SHIFT["status_mix"]), list(SHIFT["status_mix"].values()))[0]
        payer = r.choices(list(SHIFT["payer_mix"]), list(SHIFT["payer_mix"].values()))[0]
        service = r.choices(list(SHIFT["service_mix"]), list(SHIFT["service_mix"].values()))[0]
        hours = math.exp(r.uniform(math.log(SHIFT["hours_range"][0]), math.log(SHIFT["hours_range"][1])))
        unresolved = min(4, int(r.expovariate(.65)))
        failures = min(3, int(r.expovariate(1.15)))
        tasks = min(4, int(r.expovariate(.8)))
        median = max(1., SHIFT["status_delay"][status] + SHIFT["payer_delay"][payer] + SHIFT["service_delay"][service] + 10*unresolved + 8*failures + 7*tasks)
        duration = r.lognormvariate(math.log(median), SHIFT["duration_noise_sigma"])
        rows.append({"hours_remaining": hours, "payer": payer, "service": service,
            "case_status": status, "unresolved_document_count": unresolved,
            "workflow_failure_count": failures, "pending_task_count": tasks})
        labels.append(int(duration > hours))
    return rows, labels

def resolution_expectation(row: dict[str, Any]) -> float:
    median = max(1., TRAIN_EXPECTED["status"][row["case_status"]] + TRAIN_EXPECTED["payer"][row["payer"]] +
        TRAIN_EXPECTED["service"][row["service"]] + 10*row["unresolved_document_count"] +
        8*row["workflow_failure_count"] + 7*row["pending_task_count"])
    # Mean of a log-normal duration under the training simulator's sigma=.55.
    return median * math.exp(.55**2 / 2)

def metrics(y, p, threshold):
    pred = (np.asarray(p) >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel().tolist()
    bins=[]
    for lo in np.linspace(0, 1, 11)[:-1]:
        idx=[i for i,v in enumerate(p) if lo <= v < lo+.1 or (lo > .89 and v == 1)]
        if idx: bins.append({"lower":round(float(lo),1),"upper":round(float(lo+.1),1),"n":len(idx),
            "mean_predicted":round(float(np.mean(np.asarray(p)[idx])),6),"observed_rate":round(float(np.mean(np.asarray(y)[idx])),6)})
    return {"threshold":float(threshold),"accuracy":float(accuracy_score(y,pred)),
        "precision":float(precision_score(y,pred,zero_division=0)),"recall":float(recall_score(y,pred,zero_division=0)),
        "f1":float(f1_score(y,pred,zero_division=0)),"roc_auc":float(roc_auc_score(y,p)),
        "pr_auc":float(average_precision_score(y,p)),"confusion_matrix_actual_0_1_pred_0_1":[[tn,fp],[fn,tp]],
        "false_positive_rate":float(fp/(fp+tn)) if fp+tn else None,"brier_score":float(brier_score_loss(y,p)),
        "reliability_curve":bins,"alerts_per_100":float(100*(fp+tp)/len(y)),
        "evaluated_cases":int(len(y)),"alerts":int(fp+tp),"true_positives":int(tp),"false_positives":int(fp),
        "false_alarm_fraction_of_alerts":float(fp/(fp+tp)) if fp+tp else None,
        "positive_class_prevalence":float(np.mean(y))}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-path", type=Path, default=DEFAULT_ARTIFACT)
    args = parser.parse_args()
    artifact_path = args.artifact_path.resolve()
    rows, y = generate_synthetic_dataset(N_TRAIN, seed=SEED)
    train_x, val_x, train_y, val_y = train_test_split(rows,y,test_size=.2,random_state=SEED,stratify=y)
    test_x, test_y = shifted_data()
    forest=create_pipeline(random_state=SEED).fit(train_x,train_y)
    calibrated=CalibratedClassifierCV(estimator=create_pipeline(random_state=SEED),method="sigmoid",cv=5).fit(train_x,train_y)
    # Calibration choice is made using normal-distribution validation Brier score only.
    raw_val=forest.predict_proba(val_x)[:,1]; cal_val=calibrated.predict_proba(val_x)[:,1]
    use_cal= brier_score_loss(val_y,cal_val) < brier_score_loss(val_y,raw_val)
    rf=calibrated if use_cal else forest
    val_p=cal_val if use_cal else raw_val
    threshold=.5
    # Prespecified high-recall threshold selected on validation data.
    candidates=sorted(set([0.,.5,*map(float,val_p)]))
    eligible=[t for t in candidates if recall_score(val_y,(val_p>=t).astype(int),zero_division=0)>=.95]
    if eligible:
        threshold=max(eligible,key=lambda t:(precision_score(val_y,(val_p>=t).astype(int),zero_division=0),t))
    # Fit a distinct vectorizer on training rows only for the logistic baseline.
    vec=DictVectorizer(sparse=True).fit(train_x)
    logreg=LogisticRegression(max_iter=1000,random_state=SEED).fit(vec.transform(train_x),train_y)
    test_prob={"majority class":np.full(len(test_y),float(np.mean(train_y))),
        "deadline arithmetic rule":np.array([float(row["hours_remaining"] <= resolution_expectation(row)) for row in test_x]),
        "logistic regression":logreg.predict_proba(vec.transform(test_x))[:,1],
        "random forest" if not use_cal else "calibrated random forest":rf.predict_proba(test_x)[:,1]}
    results={}
    for name,p in test_prob.items():
        results[name]=metrics(test_y,p,threshold)
    # Keep this evaluation artifact separate from the v2 serving model and all existing reports.
    OUT.mkdir(parents=True,exist_ok=True)
    artifact_path.parent.mkdir(parents=True,exist_ok=True)
    if artifact_path.exists(): raise FileExistsError(f"Refusing to overwrite {artifact_path}")
    joblib.dump({"model_version":"sla-shifted-experiment-v1","seed":SEED,"synthetic_only":True,"classifier":rf,"calibrated":use_cal,"threshold":threshold},artifact_path)
    artifact_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    card={"title":"PA Guard experimental SLA risk model card",
        "prominent_notice":"TRAINED AND EVALUATED ON SYNTHETIC DATA. NOT VALIDATED ON REAL OUTCOMES; real-world prior-authorization outcomes have not been evaluated.",
        "intended_use":"Offline development and comparison of simulated prospective SLA-breach estimates.",
        "prohibited_uses":["Clinical decisions","Authorization approval or denial","Payer submission decisions","Production operational decisions"],
        "real_world_validation":False,"data_source":"Synthetic simulator only; no real authorization outcomes.",
        "training_configuration":{"seed":SEED,"rows":N_TRAIN,"validation_fraction":.2,"random_forest":{"n_estimators":240,"min_samples_leaf":4,"max_features":"sqrt"},"logistic_regression":{"max_iter":1000,"random_state":SEED},"threshold_validation_recall_target":.95},
        "training_seed":SEED,"training_rows":N_TRAIN,"training_class_counts":{"no_breach":int(sum(v==0 for v in train_y)),"breach":int(sum(v==1 for v in train_y))},
        "validation_rows":len(val_y),"validation_class_counts":{"no_breach":int(sum(v==0 for v in val_y)),"breach":int(sum(v==1 for v in val_y))},"shifted_test_seed":SHIFTED_SEED,"shifted_test_rows":N_SHIFTED,
        "features":{"hours_remaining":"Positive hours until the deadline at prediction time.","payer":"Synthetic payer category.","service":"Synthetic service category.","case_status":"Workflow state at prediction time.","unresolved_document_count":"Count of documents not verified.","workflow_failure_count":"Recorded workflow failures.","pending_task_count":"Open manual follow-up tasks."},
        "training_generator_assumptions":GENERATOR_ASSUMPTIONS,
        "label_generation":"Target is 1 when independently simulated future resolution duration exceeds hours_remaining. Hidden duration is excluded from features.",
        "shifted_test_configuration":SHIFT,"target":"Simulated resolution duration exceeds positive hours remaining.",
        "threshold_selection":{"rule":"Among validation thresholds with recall >= 0.95, maximize precision; tie break highest threshold. The chosen threshold is shared across every model on the final test cases.","threshold":float(threshold),"calibration_selected_by_validation_brier":bool(use_cal),
          "raw_validation_brier":float(brier_score_loss(val_y,raw_val)),"calibrated_validation_brier":float(brier_score_loss(val_y,cal_val))},
        "test_class_counts":{"no_breach":int(sum(v==0 for v in test_y)),"breach":int(sum(v==1 for v in test_y))},
        "models":results,"ml_vs_deadline_rule":{"model":"calibrated random forest" if use_cal else "random forest",
          "rule":"deadline arithmetic rule","f1_delta":results["calibrated random forest" if use_cal else "random forest"]["f1"]-results["deadline arithmetic rule"]["f1"],
          "roc_auc_delta":results["calibrated random forest" if use_cal else "random forest"]["roc_auc"]-results["deadline arithmetic rule"]["roc_auc"]},
        "deadline_arithmetic_rule":{"decision":"Alert if hours_remaining <= expected simulated resolution duration.","expected_resolution":"Training simulator's assumed log-normal mean: max(1, weighted delay sum) * exp(0.55^2/2). Uses payer, service, status, unresolved document, workflow failure, and pending task counts. No missing values occur in generated rows; production missingness is not evaluated."},
        "artifact":{"path":str(artifact_path.relative_to(BACKEND_DIR.parent.resolve())).replace("\\", "/"),"version":"sla-shifted-experiment-v1","sha256":artifact_sha256},
        "reproduction_command":f".\\.venv\\Scripts\\python.exe .\\backend\\evaluate_sla_shifted.py --artifact-path {artifact_path}",
        "limitations":[
          "Both training and shifted test labels are generated by explicit synthetic assumptions, not observed outcomes.",
          "Shifted evaluation is a simulation stress test, not evidence of real distribution-shift performance.",
          "The experiment artifact is not activated by the live prediction endpoint.",
          "The deadline rule uses the training simulator's expected duration and assumptions."],
        "generator_seeds":{"training":SEED,"shifted_test":SHIFTED_SEED}}
    CARD_JSON.write_text(json.dumps(card,indent=2)+"\n",encoding="utf-8")
    DOCS_CARD_JSON.write_text(json.dumps(card,indent=2)+"\n",encoding="utf-8")
    lines=["# PA Guard SLA model card","",f"> **{card['prominent_notice']}**","","This report is a reproducible synthetic simulation experiment. It is not real-world validation.","",
      f"Training rows: {N_TRAIN} (seed `{SEED}`), class counts `{card['training_class_counts']}`. Validation rows: {len(val_y)}. Shifted test rows: {N_SHIFTED} (separate generator seed `{SHIFTED_SEED}`), class counts `{card['test_class_counts']}`.",
      "The shifted generator changes payer mix, service mix, case-status mix, duration noise, and the sampled deadline range.","",
      "## Shifted synthetic test metrics","",f"All metrics below use the common validation-selected threshold `{threshold:.8f}` on the same shifted synthetic test cases.","",
      "| Model | Accuracy | Precision | Recall | F1 | ROC-AUC | PR-AUC | Brier | Prevalence | FPR | Alerts | Alerts / 100 | TP | FP | False alarm fraction | Confusion matrix |",
      "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name,m in results.items():
        alarm_fraction = f"{m['false_alarm_fraction_of_alerts']:.6f}" if m['false_alarm_fraction_of_alerts'] is not None else "N/A (no alerts)"
        lines.append(f"| {name} | {m['accuracy']:.6f} | {m['precision']:.6f} | {m['recall']:.6f} | {m['f1']:.6f} | {m['roc_auc']:.6f} | {m['pr_auc']:.6f} | {m['brier_score']:.6f} | {m['positive_class_prevalence']:.6f} | {m['false_positive_rate']:.6f} | {m['alerts']} | {m['alerts_per_100']:.6f} | {m['true_positives']} | {m['false_positives']} | {alarm_fraction} | `{m['confusion_matrix_actual_0_1_pred_0_1']}` |")
    best="calibrated random forest" if use_cal else "random forest"
    lines += ["",f"ML minus deadline arithmetic rule: F1 `{card['ml_vs_deadline_rule']['f1_delta']:+.6f}`, ROC-AUC `{card['ml_vs_deadline_rule']['roc_auc_delta']:+.6f}`.",
      "","## Reliability curve","",f"Chosen model: {best}. The points below are observed breach rate by fixed probability bin on the shifted synthetic test set.","",
      "| Bin | N | Mean predicted | Observed rate |","|---|---:|---:|---:|"]
    for b in results[best]["reliability_curve"]: lines.append(f"| [{b['lower']:.1f}, {b['upper']:.1f}) | {b['n']} | {b['mean_predicted']:.6f} | {b['observed_rate']:.6f} |")
    lines += ["","## Limitations",* [f"- {s}" for s in card["limitations"]],"","The endpoint and interface must continue to identify the served model as synthetic and unvalidated. Do not use these results to guide clinical, authorization, or payer-submission decisions.",""]
    CARD_MD.write_text("\n".join(lines),encoding="utf-8")
    print(f"Wrote {CARD_JSON}")
    print(f"Wrote {CARD_MD}")
    print(f"Wrote {artifact_path}")
    print(f"Calibration selected: {use_cal}; threshold: {threshold:.8f}; shifted test n={len(test_y)}")
    for name,m in results.items(): print(f"{name}: accuracy={m['accuracy']:.6f} precision={m['precision']:.6f} recall={m['recall']:.6f} f1={m['f1']:.6f} roc_auc={m['roc_auc']:.6f} pr_auc={m['pr_auc']:.6f} brier={m['brier_score']:.6f} prevalence={m['positive_class_prevalence']:.6f} fpr={m['false_positive_rate']:.6f} evaluated={m['evaluated_cases']} alerts={m['alerts']} alerts_per_100={m['alerts_per_100']:.6f} tp={m['true_positives']} fp={m['false_positives']} false_alarm_fraction={m['false_alarm_fraction_of_alerts'] if m['false_alarm_fraction_of_alerts'] is not None else 'N/A'} confusion={m['confusion_matrix_actual_0_1_pred_0_1']}")

if __name__ == "__main__": main()
