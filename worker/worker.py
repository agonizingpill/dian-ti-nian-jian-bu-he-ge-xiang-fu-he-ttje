import json
import os
import time
import urllib.request
import uuid
from datetime import datetime
import pymysql
import redis
from algorithm import classify

DB = {
    "host": os.getenv("MYSQL_HOST", "db"), "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "lift_user"), "password": os.getenv("MYSQL_PASSWORD", "lift_pass"),
    "database": os.getenv("MYSQL_DATABASE", "lift_review"), "cursorclass": pymysql.cursors.DictCursor, "autocommit": True,
}
BACKEND_URL = os.getenv("BACKEND_INTERNAL_URL", "http://backend:8000")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "jc1066-internal")
LEGACY_QUEUE = "lift_analysis"
REVIEW_QUEUE = "review_backend_channel"
rdb = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

def db_conn():
    return pymysql.connect(**DB)

def process(job_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM analysis_jobs WHERE id=%s", (job_id,))
            job = cur.fetchone()
            if not job:
                return
            cur.execute("UPDATE analysis_jobs SET status='running' WHERE id=%s", (job_id,))
    try:
        features = job["features_json"] if isinstance(job["features_json"], dict) else json.loads(job["features_json"])
        result = classify(features)
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE analysis_jobs SET status='completed',prediction=%s,probability=%s,path_rules_json=%s,completed_at=%s WHERE id=%s""", (result["prediction"], result["probability"], json.dumps({"rules": result["path_rules"], "model": result["model"], "features": features}, ensure_ascii=False), datetime.utcnow(), job_id))
                cur.execute("UPDATE lifts SET status=IF(status='under_review','rectification',status) WHERE id=%s", (job["lift_id"],))
    except Exception as exc:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE analysis_jobs SET status='failed',error=%s,completed_at=%s WHERE id=%s", (str(exc), datetime.utcnow(), job_id))

def post_channel_result(session_id, result):
    """把后台通道结果回调给后端；重试用同一 run_token，重复投递由后端幂等挡掉。"""
    body = json.dumps({
        "channel": "backend",
        "suggestion": result["suggestion"],
        "branch_signature": result["branch_signature"],
        "model_version": result["model_version"],
        "run_token": uuid.uuid4().hex,
    }).encode()
    url = f"{BACKEND_URL}/api/internal/review-sessions/{session_id}/channel-result"
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "X-Internal-Token": INTERNAL_TOKEN})
            urllib.request.urlopen(req, timeout=5).read()
            return True
        except Exception as exc:
            print(f"channel result post failed (attempt {attempt + 1}) session={session_id}: {exc}", flush=True)
            time.sleep(2 * (attempt + 1))
    return False  # 放弃后由补算扫描兜底

def process_review(session_id):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM review_sessions WHERE id=%s", (session_id,))
            session = cur.fetchone()
    if not session:
        return
    features = session["features_json"]
    if not isinstance(features, dict):
        features = json.loads(features)
    result = classify(features)
    post_channel_result(session_id, result)

def backfill_reviews():
    """补算：捡起缺后台结果且仍未终结的送审单（worker 停机恢复、队列消息丢失都靠它兜底）。"""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT s.id FROM review_sessions s
                  LEFT JOIN review_channel_results r ON r.session_id=s.id AND r.channel='backend'
                  WHERE s.status IN ('awaiting_channels','manual_pending') AND r.session_id IS NULL
                  ORDER BY s.id LIMIT 20""")
                ids = [row["id"] for row in cur.fetchall()]
    except Exception as exc:
        print(f"backfill scan error: {exc}", flush=True)
        return
    for session_id in ids:
        try:
            process_review(session_id)
        except Exception as exc:
            print(f"backfill process error session={session_id}: {exc}", flush=True)

def main():
    while True:
        try:
            open("/shared/worker_healthy", "w").write(datetime.utcnow().isoformat())
            message = rdb.brpop([LEGACY_QUEUE, REVIEW_QUEUE], timeout=5)
            if message:
                queue, payload = message[0], int(message[1])
                if queue == REVIEW_QUEUE:
                    process_review(payload)
                else:
                    process(payload)
            else:
                backfill_reviews()
        except Exception as exc:
            print(f"worker loop error: {exc}", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
