import os
from sklearn.tree import DecisionTreeClassifier, export_text

FEATURE_NAMES = ["critical_fail_count", "average_score", "failed_item_count", "documentation_gap"]

TRAIN_X = [
    [0, 96, 0, 0], [0, 84, 1, 0], [0, 78, 2, 0],
    [0, 72, 2, 1], [1, 96, 1, 0], [1, 68, 3, 1],
    [0, 91, 0, 1], [0, 66, 3, 1], [2, 88, 2, 0],
]
TRAIN_VARIANTS = {
    # 与接单处完全一致的基准口径
    "baseline": ["ready", "ready", "review", "hold", "hold", "hold", "review", "hold", "hold"],
    # 严格口径：模拟后台模型版本偏斜，中分段单不合格项一律人工 review，用于演示两边对不上
    "strict": ["ready", "review", "review", "hold", "hold", "hold", "review", "hold", "hold"],
}
MODEL_VARIANT = os.getenv("TREE_VARIANT", "baseline")
if MODEL_VARIANT not in TRAIN_VARIANTS:
    MODEL_VARIANT = "baseline"
MODEL_VERSION = f"dtree-1066-d3-{MODEL_VARIANT}"


def train_model():
    model = DecisionTreeClassifier(max_depth=3, random_state=1066)
    model.fit(TRAIN_X, TRAIN_VARIANTS[MODEL_VARIANT])
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


def classify(features):
    model = train_model()
    vector = [[features[n] for n in FEATURE_NAMES]]
    prediction = model.predict(vector)[0]
    return {
        "prediction": prediction,
        "suggestion": prediction,
        "probability": float(max(model.predict_proba(vector)[0])),
        "path_rules": export_text(model, feature_names=FEATURE_NAMES),
        "branch_signature": branch_signature(model, features),
        "model_version": MODEL_VERSION,
        "model": "DecisionTreeClassifier(random_state=1066,max_depth=3)",
    }
