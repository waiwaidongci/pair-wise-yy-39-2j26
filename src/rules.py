from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='大坝巡检、缺陷与应急管理'; ENTITY='大坝缺陷'; BATCH_ENTITY='巡检批次'; READING_ENTITY='巡检读数'; TASK_ENTITY='处置任务'; ID_PREFIX='DS'
SEVERITIES=['observation', 'minor', 'major', 'emergency']; STATES=['planned', 'inspected', 'defect_confirmed', 'repair', 'verified', 'closed']; TRANSITIONS={'planned': ['inspected'], 'inspected': ['defect_confirmed'], 'defect_confirmed': ['repair'], 'repair': ['verified'], 'verified': ['closed'], 'closed': []}; TRANSITION_ROLES={'inspected': ['inspector'], 'defect_confirmed': ['dam_engineer'], 'repair': ['dam_engineer'], 'verified': ['inspector'], 'closed': ['emergency_manager']}
CREATE_ROLES=set(['inspector']); RECORD_ROLES=set(['inspector', 'dam_engineer']); AUDIT_ROLES=set(['emergency_manager', 'viewer']); VIEW_ROLES=set(['inspector', 'dam_engineer', 'emergency_manager', 'viewer'])
SEVERITY_WEIGHT={'observation': 1.0, 'minor': 3.0, 'major': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'observation': 72, 'minor': 24, 'major': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
# 批次/读数/处置任务规则
BATCH_CREATE_ROLES=set(['inspector']); REVIEW_ROLES=set(['inspector', 'dam_engineer']); TASK_ACCEPT_ROLES=set(['dam_engineer', 'emergency_manager'])
METRIC_LABELS={'seepage': '渗流', 'displacement': '位移', 'crack': '裂缝'}
# 相对差超过10%视为矛盾读数，进入待复核
CONTRADICTION_TOLERANCE=0.10
# 超控制值分级：>=2倍控制值为emergency，>=1.5倍为major，其余为minor
SEVERITY_BANDS=[(2.0,'emergency'),(1.5,'major'),(1.0,'minor')]
# 处置任务默认责任人（以指标类型分派）
DEFAULT_ASSIGNEE={'seepage': 'duty_seepage_engineer', 'displacement': 'duty_monitor_engineer', 'crack': 'duty_structure_engineer'}
# 复检记录类型
REINSPECTION_KIND='reinspection'
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
# ---- 巡检批次规则 ----
def exceeds_control(value,control):
    """读数超过控制值即进入优先队列"""
    return control>0 and value>control
def breach_severity(value,control):
    """超控制值的分级：超得越多级别越高"""
    ratio=value/control if control>0 else 1.0
    for bound,severity in SEVERITY_BANDS:
        if ratio>=bound: return severity
    return 'minor'
def reading_contradicts(value,previous,tolerance=CONTRADICTION_TOLERANCE):
    """后到读数与既有读数相对差超过容差即为矛盾读数"""
    if previous==0: return abs(value)>tolerance
    return abs(value-previous)/abs(previous)>tolerance
def default_assignee(metric):
    return DEFAULT_ASSIGNEE.get(metric,'duty_engineer')
def arrival_deadline_hours(severity,value=0.0,control=1.0):
    """处置任务到场时限，复用缺陷响应时限规则"""
    return response_deadline_hours(severity,value,control)
def reinspection_required(item):
    """应急处置缺陷（紧急级别或读数达到控制值）必须复检"""
    return escalation_required(item["severity"], item["quantity"], item["threshold"])
def close_blockers(open_records,requires_reinspection,has_reinspection):
    """关闭不变量：未关闭事项、缺失复检记录都不能关闭"""
    blockers=[]
    if open_records>0: blockers.append("仍有未关闭事项")
    if requires_reinspection and not has_reinspection:
        blockers.append("复检记录缺失，不能关闭")
    return blockers
