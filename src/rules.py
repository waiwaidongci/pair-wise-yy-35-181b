from __future__ import annotations
from .domain import (ConflictError, PermissionDenied, ValidationError,
                     SIGNOFF_STATUSES)
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
# 关闭会签：辐射防护员提交随访结论与依据版本，卫生物理师换人审核
SIGNOFF_SUBMIT_ROLES=set(['radiation_officer']); SIGNOFF_REVIEW_ROLES=set(['health_physicist'])
SIGNOFF_REQUIRED_STATUS='follow_up'
SIGNOFF_INVALID_STATUS=set(['submitted','approved','rejected'])
INVALID_REASON_STATUS_CHANGED='status_changed'
INVALID_REASON_RECORDS_CHANGED='records_changed'
INVALID_REASON_LABELS={'status_changed': '事件状态已变动', 'records_changed': '随访记录已变动'}
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
def signoff_close_blocker(item, signoff):
    if 'closed' not in TRANSITIONS.get(item["status"],[]): return []
    if not signoff: return ["缺少当前版本通过的关闭会签"]
    if signoff["status"]!='approved' or signoff["invalidated_reason"] is not None:
        return ["缺少当前版本通过的关闭会签"]
    if signoff["basis_version"]!=item["version"]:
        return ["会签依据版本与当前数据不一致"]
    return []
def validate_signoff_submission(item,basis_version,current_active):
    if item["status"]!=SIGNOFF_REQUIRED_STATUS:
        raise ConflictError("只有医学随访中的事件才能提交关闭会签")
    if basis_version!=item["version"]:
        raise ConflictError("依据版本不是当前版本，请基于最新数据重新提交")
    if current_active:
        if current_active["status"]=='submitted':
            raise ConflictError("本版本会签已提交，等待卫生物理师审核")
        if current_active["status"]=='approved':
            raise ConflictError("本版本已有通过会签，可直接关闭事件")
        raise ConflictError("本版本会签已被退回，请处理退回意见后在新版本上重新提交")
def validate_signoff_review(signoff,reviewer_role,reviewer):
    if signoff["status"] not in ('submitted','rejected') or signoff["invalidated_reason"] is not None:
        raise ConflictError("该会签已处理或已失效，不能再审核")
    if reviewer_role not in SIGNOFF_REVIEW_ROLES:
        raise PermissionDenied("只有卫生物理师可以审核关闭会签")
    if reviewer==signoff["submitted_by"]:
        raise PermissionDenied("会签必须换人审核，提交人不能审核自己的会签")
def signoff_summary(signoff,current_version=None,closing=False):
    if not signoff: return None
    result={key:signoff[key] for key in ("id","basis_version","status","submitted_by","submitted_at","reviewed_by","review_comment","reviewed_at","invalidated_reason")}
    reason=signoff.get("invalidated_reason")
    result["invalidated_reason_label"]=INVALID_REASON_LABELS.get(reason) if reason else None
    result["active"]=reason is None and signoff["status"] in SIGNOFF_INVALID_STATUS
    if current_version is not None:
        # 关闭时版本会再+1，关闭所采用的会签依据视为与关闭时数据一致
        result["basis_current"]=closing or signoff["basis_version"]==current_version
    return result
