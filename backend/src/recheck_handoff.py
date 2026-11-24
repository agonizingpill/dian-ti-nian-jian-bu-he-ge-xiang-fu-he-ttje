"""Source records and completion rules for photo-replacement."""

RECHECK_HANDOFF = {
    'photo-replacement': {
        'title': '串项影像补证验收',
        'reference_label': '复检影像凭证号',
        'outcomes': {
            'fixed': ('同一整改单影像已匹配', ('matched',)),
            'escalated': ('转历史影像追溯', None),
        },
        'sql': """
SELECT CAST(n.id AS CHAR) AS source_key,CONCAT(l.asset_code,' · ',i.code,' · 整改单 ',n.id) AS subject,
       CONCAT(i.title,' · 整改状态 ',n.status,' · 有效照片复检记录 ',(
          SELECT COUNT(*) FROM rechecks r WHERE r.notice_id=n.id AND r.photo_item_code=i.code
          AND NULLIF(TRIM(r.photo_ref),'') IS NOT NULL AND r.status<>'evidence_rejected')) AS source_summary,
       CASE WHEN EXISTS (SELECT 1 FROM rechecks r WHERE r.notice_id=n.id AND r.photo_item_code=i.code
             AND NULLIF(TRIM(r.photo_ref),'') IS NOT NULL AND r.status<>'evidence_rejected')
            THEN 'matched' ELSE 'missing' END AS readiness
FROM rectification_notices n JOIN inspection_items i ON i.id=n.inspection_item_id JOIN lifts l ON l.id=n.lift_id
""",
    },
    'inspector-redispatch': {
        'title': '同档复检改约验收',
        'reference_label': '新预约回执号',
        'outcomes': {
            'clear': ('原同档冲突已解除', ('clear',)),
            'transfer': ('交给片区负责人排班', None),
        },
        'sql': """
SELECT CAST(r.id AS CHAR) AS source_key,CONCAT(r.inspector,' · ',l.asset_code,' · 复检 ',r.id) AS subject,
       CONCAT('预约 ',r.scheduled_at,' · 当前状态 ',r.status) AS source_summary,
       CASE WHEN r.status<>'scheduled' OR n.status IN ('closed','verified') OR NOT EXISTS (
            SELECT 1 FROM rechecks other JOIN rectification_notices m ON m.id=other.notice_id
            WHERE other.id<>r.id AND other.status='scheduled' AND m.status NOT IN ('closed','verified')
              AND LOWER(TRIM(other.inspector))=LOWER(TRIM(r.inspector)) AND other.scheduled_at=r.scheduled_at
       ) THEN 'clear' ELSE 'overlap' END AS readiness
FROM rechecks r JOIN rectification_notices n ON n.id=r.notice_id JOIN lifts l ON l.id=n.lift_id
""",
    },
}
