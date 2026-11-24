"""测试共享的内存替身：复刻条件 UPDATE 先落定者赢语义与 Redis 去重入队。"""
import json
from datetime import datetime, timedelta


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 24, 9, 0, 0)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


CLOCK = Clock()


class FakeStore:
    """与 review_sessions 的 db_* 函数一一对应的内存实现，保持原子条件语义。"""

    def __init__(self, open_statuses):
        self.sessions = {}
        self.results = {}
        self.lifts = {1: {"asset_code": "NJ-T-001", "building": "测试楼"}}
        self.next_id = 1
        self.audit = []
        self._open_statuses = open_statuses

    # --- session ---
    def insert_session(self, lift_id, key, features, deadline, created_by):
        if any(s["lift_id"] == lift_id and s["idempotency_key"] == key for s in self.sessions.values()):
            return None
        sid = self.next_id
        self.next_id += 1
        self.sessions[sid] = {
            "id": sid, "lift_id": lift_id, "idempotency_key": key,
            "features_json": json.dumps(features, ensure_ascii=False),
            "status": "awaiting_channels", "escalate_reason": None,
            "conclusion": None, "conclusion_source": None, "conclusion_note": None,
            "claimed_by": None, "claimed_at": None, "decided_at": None,
            "deadline_at": deadline, "created_by": created_by, "created_at": CLOCK.now,
        }
        return sid

    def get_session(self, sid):
        row = self.sessions.get(sid)
        return dict(row) if row else None

    def get_session_by_key(self, lift_id, key):
        for s in self.sessions.values():
            if s["lift_id"] == lift_id and s["idempotency_key"] == key:
                return dict(s)
        return None

    def open_session_for_lift(self, lift_id):
        for s in self.sessions.values():
            if s["lift_id"] == lift_id and s["status"] in self._open_statuses:
                return {"id": s["id"]}
        return None

    def expired_awaiting(self, now):
        return [dict(s) for s in self.sessions.values()
                if s["status"] == "awaiting_channels" and s["deadline_at"] < now]

    def list_sessions(self):
        return [{**s, **self.lifts.get(s["lift_id"], {})}
                for s in sorted(self.sessions.values(), key=lambda x: -x["id"])]

    def lift_brief(self, lift_id):
        return dict(self.lifts.get(lift_id, {}))

    # --- channel results ---
    def insert_channel_result(self, sid, channel, suggestion, branch, version, token, computed_at):
        if (sid, channel) in self.results:  # 首写胜出
            return False
        self.results[(sid, channel)] = {
            "session_id": sid, "channel": channel, "suggestion": suggestion,
            "branch_signature": branch, "model_version": version, "run_token": token,
            "reference_only": 0, "computed_at": computed_at,
        }
        return True

    def channel_results(self, sid):
        return [dict(r) for (s, _), r in self.results.items() if s == sid]

    def results_for_sessions(self, sids):
        return [dict(r) for (s, _), r in self.results.items() if s in sids]

    def mark_reference(self, sid, channel):
        if (sid, channel) in self.results:
            self.results[(sid, channel)]["reference_only"] = 1

    # --- transitions（条件 UPDATE：先落定者赢） ---
    def transition(self, sid, expect_status, sets):
        session = self.sessions[sid]
        if session["status"] != expect_status:
            return False
        session.update(sets)
        return True

    def transition_claimed(self, sid, claimer, sets):
        session = self.sessions[sid]
        if session["status"] != "manual_claimed" or session["claimed_by"] != claimer:
            return False
        session.update(sets)
        return True

    # --- 副作用记录 ---
    def log(self, actor_id, action, entity_type, entity_id, detail):
        self.audit.append({"actor_id": actor_id, "action": action, "entity_id": entity_id, "detail": detail})

    def audit_count(self, action):
        return sum(1 for a in self.audit if a["action"] == action)


class FakeRedis:
    """复刻 SET NX EX 去重 + LPUSH，验证入队侧幂等。"""

    def __init__(self, queue_name):
        self.flags = {}
        self.queue = []
        self.queue_name = queue_name

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.flags:
            return None
        self.flags[key] = value
        return True

    def lpush(self, queue, value):
        self.queue.append((queue, value))

    def queued(self, session_id):
        return self.queue.count((self.queue_name, str(session_id)))


def install(module, store, redis_client, clock=CLOCK):
    """把 review_sessions 的外部依赖全部换成内存替身，返回还原函数。"""
    patches = {
        "db_insert_session": store.insert_session,
        "db_get_session": store.get_session,
        "db_get_session_by_key": store.get_session_by_key,
        "db_insert_channel_result": store.insert_channel_result,
        "db_channel_results": store.channel_results,
        "db_transition": store.transition,
        "db_transition_claimed": store.transition_claimed,
        "db_mark_reference": store.mark_reference,
        "db_expired_awaiting": store.expired_awaiting,
        "db_open_session_for_lift": store.open_session_for_lift,
        "db_list_sessions": store.list_sessions,
        "db_results_for_sessions": store.results_for_sessions,
        "db_lift_brief": store.lift_brief,
        "log_action": store.log,
        "redis_client": redis_client,
        "_now": lambda: clock.now,
    }
    saved = {name: getattr(module, name) for name in patches}
    for name, fn in patches.items():
        setattr(module, name, fn)

    def restore():
        for name, fn in saved.items():
            setattr(module, name, fn)

    return restore
