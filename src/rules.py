from __future__ import annotations
from datetime import datetime, timedelta, timezone
from .domain import ConflictError, ValidationError
TITLE='大坝巡检、缺陷与应急管理'; ENTITY='大坝缺陷'; ID_PREFIX='DS'
SEVERITIES=['observation', 'minor', 'major', 'emergency']; STATES=['planned', 'inspected', 'defect_confirmed', 'repair', 'verified', 'closed']; TRANSITIONS={'planned': ['inspected'], 'inspected': ['defect_confirmed'], 'defect_confirmed': ['repair'], 'repair': ['verified'], 'verified': ['closed'], 'closed': []}; TRANSITION_ROLES={'inspected': ['inspector'], 'defect_confirmed': ['dam_engineer'], 'repair': ['dam_engineer'], 'verified': ['inspector'], 'closed': ['emergency_manager']}
CREATE_ROLES=set(['inspector']); RECORD_ROLES=set(['inspector', 'dam_engineer']); AUDIT_ROLES=set(['emergency_manager', 'viewer']); VIEW_ROLES=set(['inspector', 'dam_engineer', 'emergency_manager', 'viewer'])
SEVERITY_WEIGHT={'observation': 1.0, 'minor': 3.0, 'major': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'observation': 72, 'minor': 24, 'major': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
# 巡检批次：监测类型、读数状态、责任角色与默认责任人
METRICS={'seepage':'渗流','displacement':'位移','crack':'裂缝'}; METRIC_UNITS={'seepage':'L/s','displacement':'mm','crack':'mm'}
READING_STATES=('normal','priority','merged','review')
REVIEW_TOLERANCE=0.10
ASSIGNEE_ROLE={'minor':'dam_engineer','major':'dam_engineer','emergency':'emergency_manager'}
DEFAULT_ASSIGNEE={'dam_engineer':'值班坝工工程师','emergency_manager':'值班应急管理员'}
BATCH_CREATE_ROLES=set(['inspector']); BATCH_VIEW_ROLES=VIEW_ROLES; TASK_CLAIM_ROLES=set(['dam_engineer','emergency_manager'])
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
def completion_blockers(target,open_records,requires_reinspection=False,closed_reinspections=0):
    if target not in TERMINAL_STATES: return []
    blockers=[]
    if requires_reinspection and closed_reinspections<=0: blockers.append("复检记录缺失，不能关闭")
    if open_records>0: blockers.append("仍有未关闭事项")
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def metric_label(metric):
    if metric not in METRICS: raise ValidationError("metric必须是seepage、displacement或crack")
    return METRICS[metric]
def reading_over_limit(value,control): return control>0 and value>control
def reading_severity(value,control):
    """超控制值即入优先队列；超出越多等级越高。"""
    ratio=value/control if control>0 else 0.0
    if ratio>=2.0: return 'emergency'
    if ratio>=1.5: return 'major'
    if ratio>1.0: return 'minor'
    return 'observation'
def reading_priority(value,control):
    severity=reading_severity(value,control)
    return priority_score(severity,value,control,0)
def readings_conflict(first, second, tolerance=REVIEW_TOLERANCE):
    """后到读数与最早报告矛盾：超限状态、控制值或相对偏差超过容忍度即矛盾。"""
    if bool(first["over_limit"])!=bool(second["over_limit"]): return True
    c1=float(first["control_value"]); c2=float(second["control_value"])
    if c1>0 and abs(c1-c2)/c1>tolerance: return True
    v1=float(first["value"]); v2=float(second["value"])
    base=max(abs(v1),c1,1e-9)
    return abs(v1-v2)/base>tolerance
def assignee_role_for(severity):
    if severity not in ASSIGNEE_ROLE: raise ValidationError("unknown severity")
    return ASSIGNEE_ROLE[severity]
def resolve_assignee(role,roster=None):
    if roster and isinstance(roster,dict):
        name=roster.get(role)
        if isinstance(name,str) and name.strip(): return name.strip()[:100]
    return DEFAULT_ASSIGNEE[role]
def arrival_deadline(reported_at,severity,value=0.0,control=1.0):
    hours=response_deadline_hours(severity,value,control)
    start=datetime.fromisoformat(reported_at)
    if start.tzinfo is None: start=start.replace(tzinfo=timezone.utc)
    return (start+timedelta(hours=hours)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
def task_title(metric,section): return f"{metric_label(metric)}异常处置：{section}"
