"""双通道分流复核协议测试：内存 DAO 复刻条件 UPDATE 的先落定者赢语义。

运行：python3 -m unittest discover -s backend/tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from src import review_sessions as rs  # noqa: E402
import fakes  # noqa: E402
from fakes import CLOCK, FakeRedis, FakeStore  # noqa: E402

ITEMS = [
    {"result": "pass", "critical": True, "score": 94, "category": "safety"},
    {"result": "pass", "critical": True, "score": 93, "category": "safety"},
    {"result": "pass", "critical": False, "score": 90, "category": "mechanical"},
    {"result": "pass", "critical": False, "score": 87, "category": "mechanical"},
    {"result": "pass", "critical": False, "score": 96, "category": "documentation"},
]
LIFT = {"id": 1, "asset_code": "NJ-T-001"}
OPERATOR = {"sub": "1", "email": "operator@jingcheng.com", "role": "operator"}
SUPERVISOR = {"sub": "2", "email": "supervisor@jingcheng.com", "role": "supervisor"}
SUPERVISOR_B = {"sub": "3", "email": "supervisor2@jingcheng.com", "role": "supervisor"}


def intake_result():
    return rs.intake_classify(rs.build_features(ITEMS))


def backend_payload(match=True):
    intake = intake_result()
    if match:
        return {"suggestion": intake["suggestion"], "branch_signature": intake["branch_signature"],
                "model_version": intake["model_version"]}
    return {"suggestion": "review" if intake["suggestion"] != "review" else "hold",
            "branch_signature": "average_score>75.0000 → leaf:review",
            "model_version": "dtree-1066-d3-strict"}


class ReviewProtocolTest(unittest.TestCase):
    def setUp(self):
        CLOCK.now = datetime(2026, 11, 24, 9, 0, 0)
        self.store = FakeStore(rs.OPEN_STATUSES)
        self.redis = FakeRedis(rs.BACKEND_QUEUE)
        self._restore = fakes.install(rs, self.store, self.redis, CLOCK)

    def tearDown(self):
        self._restore()

    def submit(self, key="idem-0001"):
        return rs.submit(LIFT, ITEMS, key, OPERATOR)

    def deliver_backend(self, sid, match=True, token="run-token-1"):
        payload = backend_payload(match)
        return rs.record_channel_result(sid, "backend", payload["suggestion"],
                                        payload["branch_signature"], payload["model_version"], token)

    # 1. 两边对上 → 自动出结论，且只发一次
    def test_match_auto_concludes_once(self):
        view, replayed = self.submit()
        self.assertFalse(replayed)
        self.assertEqual(view["status"], "awaiting_channels")
        self.assertEqual(view["channels"], {"intake": "done", "backend": "pending"})
        view = self.deliver_backend(view["id"])
        self.assertEqual(view["status"], "auto_concluded")
        self.assertEqual(view["conclusion"], intake_result()["suggestion"])
        self.assertEqual(view["conclusion_source"], "auto")
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 1)
        self.assertEqual(self.store.audit_count("review_escalated"), 0)

    # 2. 建议对不上 → 转人工，工作台看得出是分歧
    def test_divergence_escalates_to_manual(self):
        view, _ = self.submit()
        view = self.deliver_backend(view["id"], match=False)
        self.assertEqual(view["status"], "manual_pending")
        self.assertEqual(view["escalate_reason"], "diverged")
        self.assertTrue(view["diverged"])
        self.assertIsNone(view["conclusion"])
        self.assertEqual(self.store.audit_count("review_escalated"), 1)
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 0)

    # 3. 建议相同但命中分支不同 → 也算对不上
    def test_branch_mismatch_alone_escalates(self):
        view, _ = self.submit()
        intake = intake_result()
        rs.record_channel_result(view["id"], "backend", intake["suggestion"],
                                 "average_score>75.0000 → 另一棵树的另一条分支 → leaf:" + intake["suggestion"],
                                 "dtree-1066-d3-strict", "run-token-x")
        session = self.store.get_session(view["id"])
        self.assertEqual(session["status"], "manual_pending")
        self.assertEqual(session["escalate_reason"], "diverged")

    # 4. 等满半分钟缺后台 → 转人工并标出哪边没算完；重复扫尾不重复转人工
    def test_timeout_escalates_and_sweep_idempotent(self):
        view, _ = self.submit()
        CLOCK.advance(31)
        rs.sweep_expired()
        session = self.store.get_session(view["id"])
        self.assertEqual(session["status"], "manual_pending")
        self.assertEqual(session["escalate_reason"], "backend_timeout")
        listed = rs.list_sessions()[0]
        self.assertEqual(listed["missing_channels"], ["backend"])
        self.assertEqual(listed["channels"]["backend"], "pending")
        escalations = self.store.audit_count("review_escalated")
        rs.sweep_expired()
        rs.sweep_expired()
        self.assertEqual(self.store.audit_count("review_escalated"), escalations)

    # 5. 超时后补算对上 → 收回还没人接手的人工单并自动结论
    def test_late_match_retracts_unclaimed_ticket(self):
        view, _ = self.submit()
        CLOCK.advance(31)
        rs.sweep_expired()
        self.assertEqual(self.store.get_session(view["id"])["status"], "manual_pending")
        view = self.deliver_backend(view["id"])
        self.assertEqual(view["status"], "auto_concluded")
        self.assertEqual(view["conclusion_source"], "auto")
        self.assertIsNone(view["escalate_reason"])
        self.assertEqual(self.store.audit_count("review_ticket_retracted"), 1)
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 1)
        backend_row = self.store.results[(view["id"], "backend")]
        self.assertEqual(backend_row["reference_only"], 0)

    # 6. 主管先接手 → 迟来的补算只作参考，不收回
    def test_claim_wins_then_late_result_is_reference_only(self):
        view, _ = self.submit()
        CLOCK.advance(31)
        rs.sweep_expired()
        claimed = rs.claim(view["id"], SUPERVISOR)
        self.assertEqual(claimed["status"], "manual_claimed")
        view = self.deliver_backend(view["id"])
        self.assertEqual(view["status"], "manual_claimed")
        self.assertIsNone(view["conclusion"])
        backend_row = self.store.results[(view["id"], "backend")]
        self.assertEqual(backend_row["reference_only"], 1)
        self.assertEqual(self.store.audit_count("review_late_reference"), 1)
        self.assertEqual(self.store.audit_count("review_ticket_retracted"), 0)

    # 7. 补算先落定收回 → 主管接手被拒（先落定者赢的另一面）
    def test_retract_wins_then_claim_rejected(self):
        view, _ = self.submit()
        CLOCK.advance(31)
        rs.sweep_expired()
        self.deliver_backend(view["id"])
        with self.assertRaises(Exception) as ctx:
            rs.claim(view["id"], SUPERVISOR)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)

    # 8. 同幂等键重试 → 同一送审单，不重复建单、不重复入队、不重复结论
    def test_duplicate_submit_replays(self):
        view, _ = self.submit("idem-retry")
        replay, replayed = self.submit("idem-retry")
        self.assertTrue(replayed)
        self.assertEqual(replay["id"], view["id"])
        self.assertEqual(len(self.store.sessions), 1)
        self.assertEqual(self.redis.queued(view["id"]), 1)
        self.deliver_backend(view["id"])
        replay2, _ = self.submit("idem-retry")
        self.assertEqual(replay2["status"], "auto_concluded")
        self.assertEqual(self.redis.queued(view["id"]), 1)
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 1)

    # 9. 提交中断（只落了会话行）→ 同键重试补齐接单处结果并补入队
    def test_interrupted_submit_heals_on_retry(self):
        features = rs.build_features(ITEMS)
        deadline = CLOCK.now + timedelta(seconds=rs.REVIEW_TIMEOUT_SECONDS)
        sid = self.store.insert_session(LIFT["id"], "idem-crash", features, deadline, OPERATOR["email"])
        self.assertEqual(self.store.channel_results(sid), [])
        view, replayed = self.submit("idem-crash")
        self.assertTrue(replayed)
        self.assertEqual(view["id"], sid)
        self.assertEqual(view["channels"]["intake"], "done")
        self.assertEqual(self.redis.queued(sid), 1)
        view = self.deliver_backend(sid)
        self.assertEqual(view["status"], "auto_concluded")
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 1)

    # 10. 后台结果重复投递（同 token / 新 token / 重复入队）→ 不重复结算、不重复结论
    def test_duplicate_channel_delivery_is_noop(self):
        view, _ = self.submit()
        self.deliver_backend(view["id"], token="run-token-dup")
        self.deliver_backend(view["id"], token="run-token-dup")   # 同 token 重投
        self.deliver_backend(view["id"], token="run-token-new")   # worker 重算后新 token
        self.assertEqual(len(self.store.channel_results(view["id"])), 2)
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 1)
        self.assertEqual(self.store.get_session(view["id"])["status"], "auto_concluded")

    # 11. 接手幂等：本人重试返回现状，他人接手被拒
    def test_claim_idempotent_per_supervisor(self):
        view, _ = self.submit()
        CLOCK.advance(31)
        rs.sweep_expired()
        rs.claim(view["id"], SUPERVISOR)
        again = rs.claim(view["id"], SUPERVISOR)
        self.assertEqual(again["status"], "manual_claimed")
        self.assertEqual(self.store.audit_count("review_claimed"), 1)
        with self.assertRaises(Exception) as ctx:
            rs.claim(view["id"], SUPERVISOR_B)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)

    # 12. 人工结论：接手人可落定；同结论重试幂等；不同结论或他人 → 409
    def test_manual_conclude_idempotent(self):
        view, _ = self.submit()
        CLOCK.advance(31)
        rs.sweep_expired()
        rs.claim(view["id"], SUPERVISOR)
        done = rs.conclude_manual(view["id"], SUPERVISOR, "review", "两边结果已核对，人工判定")
        self.assertEqual(done["status"], "manual_closed")
        self.assertEqual(done["conclusion"], "review")
        retry = rs.conclude_manual(view["id"], SUPERVISOR, "review", "两边结果已核对，人工判定")
        self.assertEqual(retry["status"], "manual_closed")
        self.assertEqual(self.store.audit_count("review_manual_concluded"), 1)
        with self.assertRaises(Exception) as ctx:
            rs.conclude_manual(view["id"], SUPERVISOR, "hold", "想改结论")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)
        with self.assertRaises(Exception) as ctx:
            rs.conclude_manual(view["id"], SUPERVISOR_B, "hold", "他人不能结论")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)

    # 13. 未转人工前不能接手
    def test_claim_before_escalation_rejected(self):
        view, _ = self.submit()
        with self.assertRaises(Exception) as ctx:
            rs.claim(view["id"], SUPERVISOR)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)

    # 14. 同一设备已有进行中的送审单 → 不同幂等键也不能重复送审
    def test_open_session_blocks_second_submit(self):
        self.submit("idem-a")
        with self.assertRaises(Exception) as ctx:
            self.submit("idem-b")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)

    # 15. 工作台投影：分歧与缺边一眼可辨
    def test_list_projection_shows_divergence_and_missing_side(self):
        view, _ = self.submit("idem-div")
        self.deliver_backend(view["id"], match=False)
        failing_items = [{"result": "fail", "critical": True, "score": 42, "category": "safety"}] + ITEMS[1:]
        self.store.lifts[2] = {"asset_code": "NJ-T-002", "building": "测试二号楼"}
        lift2 = {"id": 2, "asset_code": "NJ-T-002"}
        rs.submit(lift2, failing_items, "idem-pending", OPERATOR)
        listed = {s["id"]: s for s in rs.list_sessions()}
        self.assertTrue(listed[view["id"]]["diverged"])
        pending = listed[2]
        self.assertFalse(pending["diverged"])
        self.assertEqual(pending["channels"], {"intake": "done", "backend": "pending"})
        self.assertEqual(pending["missing_channels"], ["backend"])

    # 16. 边界：补算结果落库时扫尾已先把会话转人工 → 结算跟一轮收回
    def test_settle_follows_through_when_already_escalated(self):
        view, _ = self.submit()
        sid = view["id"]
        payload = backend_payload(match=True)
        self.store.insert_channel_result(sid, "backend", payload["suggestion"],
                                         payload["branch_signature"], payload["model_version"],
                                         "tok-boundary", CLOCK.now)
        CLOCK.advance(31)
        self.store.transition(sid, "awaiting_channels",
                              {"status": "manual_pending", "escalate_reason": "backend_timeout"})
        rs.settle(sid, arrived_channel="backend")
        session = self.store.get_session(sid)
        self.assertEqual(session["status"], "auto_concluded")
        self.assertEqual(self.store.audit_count("review_ticket_retracted"), 1)
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 1)

    # 17. 边界竞态：补算线程读到 awaiting 但条件翻转输给扫尾 → 跟一轮按新状态收回
    def test_settle_follows_through_after_losing_awaiting_transition(self):
        view, _ = self.submit()
        sid = view["id"]
        payload = backend_payload(match=True)
        self.store.insert_channel_result(sid, "backend", payload["suggestion"],
                                         payload["branch_signature"], payload["model_version"],
                                         "tok-race", CLOCK.now)
        CLOCK.advance(31)
        original = self.store.transition
        raced = {"done": False}

        def racing_transition(s, expect, sets):
            if (not raced["done"] and expect == "awaiting_channels"
                    and sets.get("status") == "auto_concluded"):
                raced["done"] = True
                original(sid, "awaiting_channels",
                         {"status": "manual_pending", "escalate_reason": "backend_timeout"})
                return False  # 扫尾线程先落定
            return original(s, expect, sets)

        rs.db_transition = racing_transition
        try:
            rs.settle(sid, arrived_channel="backend")
        finally:
            rs.db_transition = self.store.transition
        session = self.store.get_session(sid)
        self.assertEqual(session["status"], "auto_concluded")
        self.assertEqual(self.store.audit_count("review_ticket_retracted"), 1)
        self.assertEqual(self.store.audit_count("review_auto_concluded"), 1)


if __name__ == "__main__":
    unittest.main()
