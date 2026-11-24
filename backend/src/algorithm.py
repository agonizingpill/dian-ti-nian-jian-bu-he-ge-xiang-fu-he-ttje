import json
from sklearn.tree import DecisionTreeClassifier, export_text

FEATURE_NAMES = ["critical_fail_count", "average_score", "failed_item_count", "documentation_gap"]
MODEL_VARIANT = "baseline"
MODEL_VERSION = f"dtree-1066-d3-{MODEL_VARIANT}"

# Reproducible training set mirrors the regulator's triage labels for a compact rule extractor.
TRAIN_X = [
    [0, 96, 0, 0], [0, 84, 1, 0], [0, 78, 2, 0],
    [0, 72, 2, 1], [1, 96, 1, 0], [1, 68, 3, 1],
    [0, 91, 0, 1], [0, 66, 3, 1], [2, 88, 2, 0],
]
TRAIN_Y = ["ready", "ready", "review", "hold", "hold", "hold", "review", "hold", "hold"]


def train_model():
    model = DecisionTreeClassifier(max_depth=3, random_state=1066)
    model.fit(TRAIN_X, TRAIN_Y)
    return model


def branch_signature(model, features):
    """Canonical hit-branch path for one feature vector, e.g. 'a<=0.5000 → b>77.5000 → leaf:hold'."""
    tree = model.tree_
    vector = [float(features[name]) for name in FEATURE_NAMES]
    node, steps = 0, []
    while tree.children_left[node] != tree.children_right[node]:
        idx = tree.feature[node]
        threshold = float(tree.threshold[node])
        go_left = vector[idx] <= threshold
        steps.append(f"{FEATURE_NAMES[idx]}{'<=' if go_left else '>'}{threshold:.4f}")
        node = tree.children_left[node] if go_left else tree.children_right[node]
    leaf = model.classes_[tree.value[node].argmax()]
    steps.append(f"leaf:{leaf}")
    return " → ".join(steps)


def classify(features: dict):
    model = train_model()
    vector = [[features[n] for n in FEATURE_NAMES]]
    prediction = model.predict(vector)[0]
    probability = float(max(model.predict_proba(vector)[0]))
    return {
        "prediction": prediction,
        "suggestion": prediction,
        "probability": probability,
        "path_rules": export_text(model, feature_names=FEATURE_NAMES),
        "branch_signature": branch_signature(model, features),
        "model_version": MODEL_VERSION,
        "features": features,
        "model": "DecisionTreeClassifier(random_state=1066,max_depth=3)",
    }


def build_features(items):
    failed = [x for x in items if x["result"] == "fail"]
    scores = [float(x["score"]) for x in items if x["result"] != "na"]
    return {
        "critical_fail_count": sum(1 for x in failed if x["critical"]),
        "average_score": round(sum(scores) / len(scores), 2) if scores else 0,
        "failed_item_count": len(failed),
        "documentation_gap": sum(1 for x in items if x["category"] == "documentation" and x["result"] == "fail"),
    }
