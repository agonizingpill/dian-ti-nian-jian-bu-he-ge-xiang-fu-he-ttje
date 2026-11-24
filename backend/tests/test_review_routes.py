"""HTTP 层测试：送审、回调鉴权、角色控制、超时转人工、接手与结论的完整链路。

运行：python3 -m unittest discover -s backend/tests -v
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient  # noqa: E402

from src import review_routes as routes  # noqa: E402
from src import review_sessions as rs  # noqa: E402
from src.app import app  # noqa: E402
from src.auth import create_token  # noqa: E402
import fakes  # noqa: E402
from fakes import CLOCK, FakeRedis, FakeStore  # noqa: E402

ITEMS = [
    {"result": "pass", "critical": True, "score": 94, "category": "safety"},
    {"result": "pass", "critical": True, "score": 93, "category": "safety"},
    {"result": "pass", "critical": False, "score": 90, "category": "mechanical"},
    {"result": "pass", "critical": False, "score": 87, "category": "mechanical"},
    {"result": "pass", "critical": False, "score": 96, "category": "documentation"},
]
USERS = {
    "operator": {"id": 1, "email": "operator@jingcheng.com", "name": "李倩", "role": "operator"},
    "supervisor": {"id": 2, "email": "supervisor@jingcheng.com", "name": "周岚", "role": "supervisor"},
    "viewer": {"id": 3, "email": "viewer@jingcheng.com", "name": "顾言", "role": "viewer"},
}
INTERNAL_TOKEN = routes.INTERNAL_TOKEN


def token(role):
    return f"Bearer {create_token(USERS[role])}"


def backend_body(match=True):
    intake = rs.intake_classify(rs.build_features(ITEMS))
    if match:
        return {"channel": "backend", "suggestion": intake["suggestion"],
                "branch_signature": intake["branch_signature"],
                "model_version": intake["model_version"], "run_token": "run-token-http-1"}
    return {"channel": "backend", "suggestion": "review",
            "branch_signature": "average_score>75.0000 → leaf:review",
            "model_version": "dtree-1066-d3-strict", "run_token": "run-token-http-2"}


class ReviewRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        CLOCK.now = datetime(2026, 11, 24, 9, 0, 0)
        self.store = FakeStore(rs.OPEN_STATUSES)
        self.redis = FakeRedis(rs.BACKEND_QUEUE)
        self._restore = fakes.install(rs, self.store, self.redis, CLOCK)
        self._saved_lift = routes.fetch_lift
        self._saved_items = routes.fetch_items
        routes.fetch_lift = lambda lift_id: {"id": 1, "asset_code": "NJ-T-001"} if lift_id == 1 else None
        routes.fetch_items = lambda lift_id: list(ITEMS)

    def tearDown(self):
        self._restore()
        routes.fetch_lift = self._saved_lift
        routes.fetch_items = self._saved_items

    def submit(self, key="idem-http-1", role="operator"):
        return self.client.post("/api/review-sessions", json={"lift_id": 1, "idempotency_key": key},
                                headers={"Authorization": token(role)})

    def test_full_match_flow_over_http(self):
        res = self.submit()
        self.assertEqual(res.status_code, 201, res.text)
        view = res.json()
        self.assertEqual(view["status"], "awaiting_channels")
        self.assertFalse(view["replayed"])
        sid = view["id"]

        listed = self.client.get("/api/review-sessions", headers={"Authorization": token("viewer")}).json()
        self.assertEqual(listed["sessions"][0]["channels"]["backend"], "pending")
        self.assertEqual(listed["timeout_seconds"], rs.REVIEW_TIMEOUT_SECONDS)

        denied = self.client.post(f"/api/internal/review-sessions/{sid}/channel-result",
                                  json=backend_body(), headers={"X-Internal-Token": "wrong"})
        self.assertEqual(denied.status_code, 403)

        ok = self.client.post(f"/api/internal/review-sessions/{sid}/channel-result",
                              json=backend_body(), headers={"X-Internal-Token": INTERNAL_TOKEN})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["status"], "auto_concluded")

        # 回调重投（网络重试）不重复发结论
        again = self.client.post(f"/api/internal/review-sessions/{sid}/channel-result",
                                 json=backend_body(), headers={"X-Internal-Token": INTERNAL_TOKEN})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 1)

    def test_duplicate_submit_same_key_replays(self):
        first = self.submit("idem-http-dup").json()
        second = self.submit("idem-http-dup").json()
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(self.store.sessions), 1)

    def test_viewer_cannot_submit_and_operator_cannot_claim(self):
        self.assertEqual(self.submit("idem-http-2", role="viewer").status_code, 403)
        sid = self.submit("idem-http-3").json()["id"]
        CLOCK.advance(31)
        self.client.get("/api/review-sessions", headers={"Authorization": token("viewer")})  # 惰性扫尾
        claim = self.client.post(f"/api/review-sessions/{sid}/claim",
                                 headers={"Authorization": token("operator")})
        self.assertEqual(claim.status_code, 403)

    def test_timeout_then_supervisor_claim_and_conclude(self):
        sid = self.submit("idem-http-4").json()["id"]
        CLOCK.advance(31)
        listed = self.client.get("/api/review-sessions", headers={"Authorization": token("supervisor")}).json()
        session = listed["sessions"][0]
        self.assertEqual(session["status"], "manual_pending")
        self.assertEqual(session["escalate_reason"], "backend_timeout")
        self.assertEqual(session["missing_channels"], ["backend"])

        claimed = self.client.post(f"/api/review-sessions/{sid}/claim",
                                   headers={"Authorization": token("supervisor")})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["status"], "manual_claimed")

        # 接手后迟到的后台结果只作参考
        late = self.client.post(f"/api/internal/review-sessions/{sid}/channel-result",
                                json=backend_body(), headers={"X-Internal-Token": INTERNAL_TOKEN})
        self.assertEqual(late.json()["status"], "manual_claimed")
        detail = self.client.get(f"/api/review-sessions/{sid}",
                                 headers={"Authorization": token("supervisor")}).json()
        backend_row = next(r for r in detail["channel_results"] if r["channel"] == "backend")
        self.assertTrue(backend_row["reference_only"])

        done = self.client.post(f"/api/review-sessions/{sid}/conclude",
                                json={"conclusion": "review", "note": "两边结果已核对，人工判定"},
                                headers={"Authorization": token("supervisor")})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["status"], "manual_closed")
        self.assertEqual(done.json()["conclusion_source"], "manual")
        retry = self.client.post(f"/api/review-sessions/{sid}/conclude",
                                 json={"conclusion": "review", "note": "两边结果已核对，人工判定"},
                                 headers={"Authorization": token("supervisor")})
        self.assertEqual(retry.status_code, 200)  # 同结论重试幂等
        self.assertEqual(self.store.audit_count("review_manual_concluded"), 1)

    def test_divergence_visible_in_list(self):
        sid = self.submit("idem-http-5").json()["id"]
        self.client.post(f"/api/internal/review-sessions/{sid}/channel-result",
                         json=backend_body(match=False), headers={"X-Internal-Token": INTERNAL_TOKEN})
        listed = self.client.get("/api/review-sessions", headers={"Authorization": token("viewer")}).json()
        session = listed["sessions"][0]
        self.assertEqual(session["status"], "manual_pending")
        self.assertTrue(session["diverged"])
        self.assertEqual(session["escalate_reason"], "diverged")

    def test_validation_and_auth(self):
        no_auth = self.client.post("/api/review-sessions", json={"lift_id": 1, "idempotency_key": "idem-http-6"})
        self.assertEqual(no_auth.status_code, 401)
        short_key = self.client.post("/api/review-sessions", json={"lift_id": 1, "idempotency_key": "abc"},
                                     headers={"Authorization": token("operator")})
        self.assertEqual(short_key.status_code, 422)
        missing_lift = self.client.post("/api/review-sessions", json={"lift_id": 99, "idempotency_key": "idem-http-7"},
                                        headers={"Authorization": token("operator")})
        self.assertEqual(missing_lift.status_code, 404)


if __name__ == "__main__":
    unittest.main()
