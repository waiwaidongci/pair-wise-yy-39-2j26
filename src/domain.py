from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['observation', 'minor', 'major', 'emergency']; STATES=['planned', 'inspected', 'defect_confirmed', 'repair', 'verified', 'closed']; ROLES=['inspector', 'dam_engineer', 'emergency_manager', 'viewer']
# 巡检批次：读数类型、读数状态（待复核/已确认）、处置任务状态
METRICS=['seepage','displacement','crack']; READING_STATES=['pending_review','confirmed']; TASK_STATES=['open','accepted','done']; QUEUE_FILTERS=['priority','review']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def normalize_metric(value):
    if value not in METRICS: raise ValidationError("metric必须是seepage、displacement或crack之一")
    return value
def normalize_reading_state(value):
    if value not in READING_STATES: raise ValidationError("读数状态不合法")
    return value
def normalize_task_state(value):
    if value not in TASK_STATES: raise ValidationError("任务状态不合法")
    return value
def validate_queue(value):
    if value not in QUEUE_FILTERS: raise ValidationError("queue必须是priority或review")
    return value
def parse_iso8601(value,field):
    value=require_text(value,field,40)
    try:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field}必须是ISO8601时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field}必须带时区")
    return parsed
def require_int(value,field,minimum=1):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是整数")
    if isinstance(value,float) and not value.is_integer():
        raise ValidationError(f"{field}必须是整数")
    try:
        number=int(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是整数")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
