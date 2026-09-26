from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
# 关闭会签：辐射防护员提交随访结论与依据版本，卫生物理师换人复核
SIGNOFF_STATUSES=['submitted', 'approved', 'rejected', 'invalidated']; SIGNOFF_ACTIVE=set(['submitted', 'approved']); SIGNOFF_DECISIONS={'approve': 'approved', 'reject': 'rejected'}
SIGNOFF_SUBMIT_ROLES=set(['radiation_officer']); SIGNOFF_REVIEW_ROLES=set(['health_physicist'])
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
def roles_for_signoff(action):
    return SIGNOFF_SUBMIT_ROLES if action=='submit' else SIGNOFF_REVIEW_ROLES
def can_submit_signoff(status): return status=='follow_up'
def can_review_signoff(status): return status=='follow_up'
def validate_signoff_decision(decision):
    if decision not in SIGNOFF_DECISIONS: raise ValidationError("decision必须是approve或reject")
    return SIGNOFF_DECISIONS[decision]
def different_reviewer(submitted_by,reviewed_by):
    return submitted_by != reviewed_by
def signoff_is_current(signoff,current_version):
    return bool(signoff) and signoff.get('status')=='approved' and signoff.get('basis_version')==current_version
def signoff_blockers(signoff,current_version):
    if not signoff: return ["缺少关闭会签"]
    status=signoff.get('status')
    if status=='submitted': return ["关闭会签尚待卫生物理师复核"]
    if status=='rejected': return ["关闭会签已退回，需在新版本上重新提交"]
    if status=='invalidated': return [f"关闭会签已失效（{signoff.get('invalidated_reason') or '依据版本已变动'}），需重新办理"]
    if status=='approved' and signoff.get('basis_version')!=current_version: return ["关闭会签依据的版本已过期，需重新办理"]
    return []
def completion_blockers(target,open_records,signoff=None,current_version=None):
    blockers=[]
    if target in TERMINAL_STATES:
        if open_records>0: blockers.append("仍有未关闭事项")
        blockers.extend(signoff_blockers(signoff,current_version))
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
