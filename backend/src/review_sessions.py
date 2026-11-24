"""双通道分流复核：接单处与后台的分流树各算一遍同一份送审数据。

协议要点：
- 两边「建议 + 命中分支」都对上才自动出结论；对不上或等满 REVIEW_TIMEOUT_SECONDS 秒转人工。
- 所有状态翻转都是按期望状态的条件 UPDATE，先落定者赢——主管接手与补算并发时按先落定的处理。
- 通道结果按 (session_id, channel) 首写胜出，中断重试、重复投递、重复入队都不会重复结算。
- 超时后补算对上可收回还没人接手的人工单；已接手的，迟来建议只作参考（reference_only）。
"""

import json
import os
import uuid
from datetime import datetime, timedelta

import redis
from fastapi import HTTPException

from .algorithm import build_features, classify as intake_classify
from .db import connection
from .utils.audit import log_action

CHANNELS = ("intake", "backend")
CHANNEL_LABELS = {"intake": "接单处", "backend": "后台"}
SUGGESTIONS = ("ready", "review", "hold")
OPEN_STATUSES = ("awaiting_channels", "manual_pending", "manual_claimed")
REVIEW_TIMEOUT_SECONDS = int(os.getenv("REVIEW_TIMEOUT_SECONDS", "30"))
BACKEND_QUEUE = "review_backend_channel"
ENQUEUE_FLAG_TTL_SECONDS = 3600

redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)


def _now():
    return datetime.utcnow()


# ---------- 数据访问（每个函数对应一条原子 SQL，并发语义由条件 UPDATE 保证） ----------

def _execute(sql, params=()):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.lastrowid, cur.rowcount


def _one(sql, params=()):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def _all(sql, params=()):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def db_insert_session(lift_id, idem_key, features, deadline_at, created_by):
    """返回新送审单 id；同一 (lift_id, idempotency_key) 已存在时返回 None。"""
    try:
        lastrowid, _ = _execute(
            "INSERT INTO review_sessions(lift_id,idempotency_key,features_json,deadline_at,created_by)"
            " VALUES(%s,%s,%s,%s,%s)",
            (lift_id, idem_key, json.dumps(features, ensure_ascii=False), deadline_at, created_by))
        return lastrowid
    except Exception as exc:
        if "Duplicate" in str(exc):
            return None
        raise


def db_get_session(session_id):
    return _one("SELECT * FROM review_sessions WHERE id=%s", (session_id,))


def db_get_session_by_key(lift_id, idem_key):
    return _one("SELECT * FROM review_sessions WHERE lift_id=%s AND idempotency_key=%s", (lift_id, idem_key))


def db_insert_channel_result(session_id, channel, suggestion, branch_signature, model_version, run_token, computed_at):
    """首写胜出：True 表示本次写入落定，False 表示该通道已有结果（重复投递/重试）。"""
    _execute(
        "INSERT INTO review_channel_results"
        "(session_id,channel,suggestion,branch_signature,model_version,run_token,computed_at)"
        " VALUES(%s,%s,%s,%s,%s,%s,%s)"
        " ON DUPLICATE KEY UPDATE session_id=session_id",
        (session_id, channel, suggestion, branch_signature, model_version, run_token, computed_at))
    saved = _one(
        "SELECT run_token FROM review_channel_results WHERE session_id=%s AND channel=%s",
        (session_id, channel))
    return bool(saved) and saved["run_token"] == run_token


def db_channel_results(session_id):
    return _all("SELECT * FROM review_channel_results WHERE session_id=%s ORDER BY computed_at,channel", (session_id,))


def db_transition(session_id, expect_status, sets):
    """按期望状态翻转，只有先落定的一方返回 True。"""
    assignments = ", ".join(f"{key}=%s" for key in sets)
    _, rowcount = _execute(
        f"UPDATE review_sessions SET {assignments} WHERE id=%s AND status=%s",
        (*sets.values(), session_id, expect_status))
    return rowcount == 1


def db_transition_claimed(session_id, claimer, sets):
    """只有当前接手人本人可以落定结论。"""
    assignments = ", ".join(f"{key}=%s" for key in sets)
    _, rowcount = _execute(
        f"UPDATE review_sessions SET {assignments} WHERE id=%s AND status='manual_claimed' AND claimed_by=%s",
        (*sets.values(), session_id, claimer))
    return rowcount == 1


def db_mark_reference(session_id, channel):
    _execute(
        "UPDATE review_channel_results SET reference_only=1 WHERE session_id=%s AND channel=%s",
        (session_id, channel))


def db_expired_awaiting(now):
    return _all("SELECT * FROM review_sessions WHERE status='awaiting_channels' AND deadline_at < %s", (now,))


def db_open_session_for_lift(lift_id):
    return _one(
        "SELECT id FROM review_sessions WHERE lift_id=%s AND status IN ('awaiting_channels','manual_pending','manual_claimed')",
        (lift_id,))


def db_list_sessions():
    return _all(
        "SELECT s.*,l.asset_code,l.building FROM review_sessions s JOIN lifts l ON l.id=s.lift_id"
        " ORDER BY s.id DESC LIMIT 200")


def db_results_for_sessions(session_ids):
    if not session_ids:
        return []
    marks = ",".join(["%s"] * len(session_ids))
    return _all(f"SELECT * FROM review_channel_results WHERE session_id IN ({marks}) ORDER BY computed_at,channel", tuple(session_ids))


def db_lift_brief(lift_id):
    return _one("SELECT asset_code,building FROM lifts WHERE id=%s", (lift_id,)) or {}


# ---------- 协议 ----------

def _match(a, b):
    return a["suggestion"] == b["suggestion"] and a["branch_signature"] == b["branch_signature"]


def _brief(results):
    return {channel: {"suggestion": r["suggestion"], "branch": r["branch_signature"], "model": r["model_version"]}
            for channel, r in results.items()}


def _expired(session):
    return session["deadline_at"] < _now()


def enqueue_backend(session_id):
    """至少一次入队且按会话去重：中断重试、补漏重放都不会堆积重复消息。

    去重标记即使丢失也只是多一条无害消息——worker 消费端按通道首写胜出，不会重复结算。
    """
    try:
        if redis_client.set(f"review_enqueued:{session_id}", "1", nx=True, ex=ENQUEUE_FLAG_TTL_SECONDS):
            redis_client.lpush(BACKEND_QUEUE, str(session_id))
    except Exception:
        pass  # worker 的补算扫描会兜底捡起缺后台结果的送审单


def settle(session_id, arrived_channel=None):
    """结算入口：通道结果到达、扫尾线程都会走到这里；所有翻转幂等，先落定者赢。

    状态迁移最多两段（awaiting → manual_pending → auto_concluded）。在状态边界上
    输掉条件翻转的线程跟一轮按新状态继续结算，保证「超时转人工」与「补算收回」
    在边界并发下无缝衔接，不会卡在已转人工但结果已到齐的中间态。
    """
    for _ in range(4):
        session = db_get_session(session_id)
        if not session:
            return
        results = {r["channel"]: r for r in db_channel_results(session_id)}
        status = session["status"]
        if status == "awaiting_channels":
            if len(results) == len(CHANNELS):
                intake, backend = results["intake"], results["backend"]
                if _match(intake, backend):
                    won = db_transition(session_id, "awaiting_channels", {
                        "status": "auto_concluded", "conclusion": intake["suggestion"],
                        "conclusion_source": "auto", "decided_at": _now()})
                    if won:
                        log_action(None, "review_auto_concluded", "review_session", session_id,
                                   {"conclusion": intake["suggestion"], "channels": _brief(results)})
                else:
                    won = db_transition(session_id, "awaiting_channels", {
                        "status": "manual_pending", "escalate_reason": "diverged"})
                    if won:
                        log_action(None, "review_escalated", "review_session", session_id,
                                   {"reason": "diverged", "channels": _brief(results)})
                if won:
                    return
                continue  # 没抢到：另一线程刚改了状态（如扫尾转人工），跟一轮按新状态结算
            if not _expired(session):
                return
            missing = [c for c in CHANNELS if c not in results]
            reason = "both_timeout" if len(missing) == 2 else f"{missing[0]}_timeout"
            won = db_transition(session_id, "awaiting_channels", {
                "status": "manual_pending", "escalate_reason": reason})
            if won:
                log_action(None, "review_escalated", "review_session", session_id,
                           {"reason": reason, "missing_channels": missing})
                return
            continue  # 同上：跟一轮
        if status == "manual_pending" and len(results) == len(CHANNELS):
            # 超时转人工后补算到齐：对上且仍无人接手则收回人工单；接手先落定则迟来建议只作参考
            intake, backend = results["intake"], results["backend"]
            if _match(intake, backend):
                if db_transition(session_id, "manual_pending", {
                        "status": "auto_concluded", "conclusion": intake["suggestion"],
                        "conclusion_source": "auto", "decided_at": _now(), "escalate_reason": None}):
                    log_action(None, "review_ticket_retracted", "review_session", session_id,
                               {"conclusion": intake["suggestion"], "channels": _brief(results)})
                    log_action(None, "review_auto_concluded", "review_session", session_id,
                               {"conclusion": intake["suggestion"], "after": "timeout"})
                elif arrived_channel:
                    db_mark_reference(session_id, arrived_channel)
                    log_action(None, "review_late_reference", "review_session", session_id,
                               {"channel": arrived_channel, "suggestion": results[arrived_channel]["suggestion"]})
            else:
                # 该翻转对每个迟到结果只会执行一次（插入闸门保证），不受 MySQL 同值更新 rowcount=0 影响
                if not db_transition(session_id, "manual_pending", {"escalate_reason": "diverged"}) and arrived_channel:
                    db_mark_reference(session_id, arrived_channel)
                    log_action(None, "review_late_reference", "review_session", session_id,
                               {"channel": arrived_channel, "suggestion": results[arrived_channel]["suggestion"]})
        return


def sweep_expired():
    """把等满时限仍缺通道结果的送审单转人工；与惰性结算并发安全，重复执行不重复转人工。"""
    for session in db_expired_awaiting(_now()):
        settle(session["id"])


def submit(lift, items, idem_key, user):
    """接单处收单：先算本处通道，再入队后台通道。中断重试按幂等键重放并补齐缺口。"""
    features = build_features(items)
    intake = intake_classify(features)
    actor = user["email"]
    existing = db_get_session_by_key(lift["id"], idem_key)
    if existing:
        heal(existing, intake)
        return session_view(existing["id"]), True
    if db_open_session_for_lift(lift["id"]):
        raise HTTPException(409, "该设备已有进行中的送审单")
    session_id = db_insert_session(lift["id"], idem_key, features, _now() + timedelta(seconds=REVIEW_TIMEOUT_SECONDS), actor)
    if session_id is None:  # 并发下同键重放，按已存在处理
        heal(db_get_session_by_key(lift["id"], idem_key), intake)
        return session_view(db_get_session_by_key(lift["id"], idem_key)["id"]), True
    db_insert_channel_result(session_id, "intake", intake["suggestion"], intake["branch_signature"],
                             intake["model_version"], uuid.uuid4().hex, _now())
    enqueue_backend(session_id)
    settle(session_id, arrived_channel="intake")
    log_action(int(user["sub"]), "review_session_created", "review_session", session_id,
               {"lift_id": lift["id"], "asset_code": lift["asset_code"], "features": features,
                "timeout_seconds": REVIEW_TIMEOUT_SECONDS})
    return session_view(session_id), False


def heal(session, intake=None):
    """补齐中断的送审：缺接单处结果就补算，仍等后台就补入队。重复入队无害（通道首写胜出）。"""
    session_id = session["id"]
    present = {r["channel"] for r in db_channel_results(session_id)}
    if "intake" not in present:
        if intake is None:
            features = session["features_json"]
            intake = intake_classify(json.loads(features) if isinstance(features, str) else features)
        db_insert_channel_result(session_id, "intake", intake["suggestion"], intake["branch_signature"],
                                 intake["model_version"], uuid.uuid4().hex, _now())
    if session["status"] == "awaiting_channels" and "backend" not in present:
        enqueue_backend(session_id)
    settle(session_id)


def record_channel_result(session_id, channel, suggestion, branch_signature, model_version, run_token):
    """后台通道回调：重复投递直接返回现状；已有人接手/已结论的，迟来建议只作参考。"""
    if channel != "backend":
        raise HTTPException(422, "内部回调只接受后台通道结果")
    if not db_get_session(session_id):
        raise HTTPException(404, "送审单不存在")
    landed = db_insert_channel_result(session_id, channel, suggestion, branch_signature,
                                      model_version, run_token, _now())
    if not landed:
        return session_view(session_id)
    fresh = db_get_session(session_id)
    if fresh["status"] in ("manual_claimed", "manual_closed", "auto_concluded"):
        db_mark_reference(session_id, channel)
        log_action(None, "review_late_reference", "review_session", session_id,
                   {"channel": channel, "suggestion": suggestion, "session_status": fresh["status"]})
    else:
        settle(session_id, arrived_channel=channel)
    return session_view(session_id)


def claim(session_id, user):
    """主管接手：与补算收回并发时按先落定的处理；本人重试幂等返回。"""
    session = db_get_session(session_id)
    if not session:
        raise HTTPException(404, "送审单不存在")
    actor = user["email"]
    if session["status"] == "manual_claimed" and session["claimed_by"] == actor:
        return session_view(session_id)
    if session["status"] != "manual_pending":
        raise HTTPException(409, "当前状态不能接手该复核单")
    if not db_transition(session_id, "manual_pending", {
            "status": "manual_claimed", "claimed_by": actor, "claimed_at": _now()}):
        raise HTTPException(409, "该复核单已被系统收回或被其他主管接手")
    log_action(int(user["sub"]), "review_claimed", "review_session", session_id, {"claimed_by": actor})
    return session_view(session_id)


def conclude_manual(session_id, user, conclusion, note):
    """接手人填写人工结论；同结论重试幂等返回，不重复发结论。"""
    if conclusion not in SUGGESTIONS:
        raise HTTPException(422, "结论必须是 ready/review/hold 之一")
    note = " ".join(note.split())[:500]
    if len(note) < 4:
        raise HTTPException(422, "请写明人工复核依据")
    session = db_get_session(session_id)
    if not session:
        raise HTTPException(404, "送审单不存在")
    actor = user["email"]
    if (session["status"] == "manual_closed" and session["claimed_by"] == actor
            and session["conclusion"] == conclusion):
        return session_view(session_id)
    if session["status"] != "manual_claimed" or session["claimed_by"] != actor:
        raise HTTPException(409, "只有接手该单的主管可以填写结论")
    if not db_transition_claimed(session_id, actor, {
            "status": "manual_closed", "conclusion": conclusion, "conclusion_source": "manual",
            "conclusion_note": note, "decided_at": _now()}):
        raise HTTPException(409, "该复核单状态已变化，请刷新")
    log_action(int(user["sub"]), "review_manual_concluded", "review_session", session_id,
               {"conclusion": conclusion, "claimed_by": actor, "note": note})
    return session_view(session_id)


# ---------- 工作台视图 ----------

def _dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value


def _project(session, results, detail=False):
    by_channel = {r["channel"]: r for r in results}
    missing = [c for c in CHANNELS if c not in by_channel]
    diverged = session["escalate_reason"] == "diverged" or (
        len(by_channel) == len(CHANNELS) and not _match(by_channel["intake"], by_channel["backend"]))
    view = {
        "id": session["id"],
        "lift_id": session["lift_id"],
        "asset_code": session.get("asset_code"),
        "building": session.get("building"),
        "status": session["status"],
        "escalate_reason": session["escalate_reason"],
        "diverged": diverged,
        "channels": {c: ("done" if c in by_channel else "pending") for c in CHANNELS},
        "missing_channels": missing,
        "timed_out": session["status"] == "awaiting_channels" and _expired(session),
        "conclusion": session["conclusion"],
        "conclusion_source": session["conclusion_source"],
        "conclusion_note": session["conclusion_note"],
        "claimed_by": session["claimed_by"],
        "claimed_at": _dt(session["claimed_at"]),
        "decided_at": _dt(session["decided_at"]),
        "deadline_at": _dt(session["deadline_at"]),
        "created_by": session["created_by"],
        "created_at": _dt(session["created_at"]),
    }
    if detail:
        features = session["features_json"]
        view["features"] = json.loads(features) if isinstance(features, str) else features
        view["channel_results"] = [{
            "channel": r["channel"],
            "channel_label": CHANNEL_LABELS.get(r["channel"], r["channel"]),
            "suggestion": r["suggestion"],
            "branch_signature": r["branch_signature"],
            "model_version": r["model_version"],
            "reference_only": bool(r["reference_only"]),
            "computed_at": _dt(r["computed_at"]),
        } for r in results]
    return view


def list_sessions():
    sweep_expired()
    sessions = db_list_sessions()
    grouped = {}
    for row in db_results_for_sessions([s["id"] for s in sessions]):
        grouped.setdefault(row["session_id"], []).append(row)
    return [_project(s, grouped.get(s["id"], [])) for s in sessions]


def session_view(session_id):
    session = db_get_session(session_id)
    if not session:
        raise HTTPException(404, "送审单不存在")
    session = {**session, **db_lift_brief(session["lift_id"])}
    return _project(session, db_channel_results(session_id), detail=True)
