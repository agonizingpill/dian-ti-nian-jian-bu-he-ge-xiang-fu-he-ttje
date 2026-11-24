"""Additional operational reconciliation rules for this project."""

RECONCILIATION_VIEWS = {
    'recheck-photo-trace': (
        '复检影像归属核验',
        '把复检照片与原不合格项编号逐一对照，识别缺影像和串项证据。',
        ['资产', '整改单号', '复检记录', '归属检验项', '照片标记项', '照片凭证', '复检状态', '证据结论'],
        """
SELECT l.asset_code,n.id,r.id,i.code,r.photo_item_code,r.photo_ref,r.status,
       CASE WHEN NULLIF(TRIM(r.photo_ref),'') IS NULL THEN '待补照片'
            WHEN NULLIF(TRIM(r.photo_item_code),'') IS NULL THEN '待标注检验项'
            WHEN UPPER(TRIM(r.photo_item_code)) <> UPPER(TRIM(i.code)) THEN '照片串项'
            ELSE '归属匹配' END
FROM rechecks r JOIN rectification_notices n ON n.id=r.notice_id
JOIN inspection_items i ON i.id=n.inspection_item_id JOIN lifts l ON l.id=n.lift_id
ORDER BY r.scheduled_at DESC,r.id
""",
    ),
    'recheck-staff-roster': (
        '复检员同档排班冲突',
        '按复检员与相同预约时点核对尚未完成的排班，重约或关闭后自动释放冲突。',
        ['复检员', '预约时点', '资产', '整改单', '复检号', '同档预约数', '排班结论'],
        """
SELECT r.inspector,r.scheduled_at,l.asset_code,n.id,r.id,
       COUNT(*) OVER (PARTITION BY LOWER(TRIM(r.inspector)),r.scheduled_at) AS booked,
       CASE WHEN COUNT(*) OVER (PARTITION BY LOWER(TRIM(r.inspector)),r.scheduled_at)>1 THEN '同档需改约'
            ELSE '可按预约复检' END
FROM rechecks r JOIN rectification_notices n ON n.id=r.notice_id JOIN lifts l ON l.id=n.lift_id
WHERE r.status='scheduled' AND n.status NOT IN ('closed','verified')
ORDER BY booked DESC,r.scheduled_at,r.id
""",
    ),
}
