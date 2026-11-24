"""Business queries over authoritative project records."""

from .operations_reconciliation import RECONCILIATION_VIEWS

VIEWS = {
    'inspection-calendar': (
        '年检到期排程',
        '按年检到期日排序并标出近 30 天到期与逾期资产，供检验员安排路线。',
        ['资产', '楼宇', '到期', '资产状态', '到期判定'],
        """
SELECT asset_code,building,inspection_due,status,
       CASE WHEN inspection_due<CURRENT_DATE THEN '已逾期' WHEN inspection_due<=CURRENT_DATE + INTERVAL 30 DAY THEN '30 天内' ELSE '常规排程' END
FROM lifts ORDER BY inspection_due,asset_code
""",
    ),
    'critical-rectification': (
        '关键项整改督办',
        '聚合关键不合格项及整改资料，未开单、过期或缺复检记录可分别交接。',
        ['资产', '检验项', '风险项', '整改单', '截止', '督办原因'],
        """
SELECT l.asset_code,i.code,i.title,n.id,n.deadline,
       CASE WHEN n.id IS NULL THEN '未开整改单' WHEN n.status IN ('closed','verified') THEN '已复核'
            WHEN n.deadline<CURRENT_DATE THEN '逾期' WHEN NOT EXISTS (SELECT 1 FROM rechecks r WHERE r.notice_id=n.id) THEN '待提供复检记录'
            ELSE '复检中' END
FROM inspection_items i JOIN lifts l ON l.id=i.lift_id LEFT JOIN rectification_notices n ON n.inspection_item_id=i.id
WHERE i.critical AND i.result='fail' ORDER BY i.lift_id,i.id,n.id
""",
    ),
    'certificate-register': (
        '证书有效性对照',
        '对照证书有效期与资产仍未验证的关键缺陷，保留主管判断依据。',
        ['证书号', '资产', '有效期至', '证书状态', '未验证关键项', '复审建议'],
        """
SELECT c.certificate_no,l.asset_code,c.valid_until,c.status,
       (SELECT COUNT(*) FROM inspection_items i WHERE i.lift_id=l.id AND i.critical AND i.result='fail' AND i.status<>'verified') AS unresolved,
       CASE WHEN c.valid_until<CURRENT_DATE THEN '复查有效期'
            WHEN EXISTS (SELECT 1 FROM inspection_items i WHERE i.lift_id=l.id AND i.critical AND i.result='fail' AND i.status<>'verified') THEN '复核关键项'
            ELSE '常规核对' END
FROM certificates c JOIN lifts l ON l.id=c.lift_id ORDER BY c.valid_until,c.id
""",
    ),
    **RECONCILIATION_VIEWS,
}
