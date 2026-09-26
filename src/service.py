from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, can_review_signoff, can_submit_signoff,
                    completion_blockers, different_reviewer, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    roles_for_signoff, signoff_is_current, validate_signoff_decision,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record, invalidated = self.repository.add_record(item_id, kind, detail, status,
                                                         external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
            "invalidated_signoff_ids": invalidated,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        signoff = self.repository.latest_signoff(item_id)
        blockers = completion_blockers(
            target, self.repository.open_record_count(item_id), signoff, item["version"])
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated, invalidated = self.repository.transition_item(
            item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "invalidated_signoff_ids": invalidated,
        })
        # 关闭动作消费通过会签而非将其失效，详情仍需展示该会签
        return self.enrich(updated, self.repository.latest_signoff(item_id))

    def submit_signoff(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, roles_for_signoff("submit"))
        actor = require_text(actor, "actor", 100)
        conclusion = require_text(payload.get("conclusion"), "conclusion")
        item = self.repository.get_item(item_id)
        if not can_submit_signoff(item["status"]):
            from .domain import ConflictError
            raise ConflictError("仅处于医学随访的事件可以提交关闭会签")
        latest = self.repository.latest_signoff(item_id)
        basis_version = item["version"]
        if latest is not None and latest["status"] == "rejected":
            if latest["basis_version"] >= basis_version:
                from .domain import ConflictError
                raise ConflictError("退回意见须在新版本数据上处理后才能重新提交")
        if latest is not None and latest["status"] == "submitted":
            from .domain import ConflictError
            raise ConflictError("已有待复核会签，不能重复提交")
        if latest is not None and signoff_is_current(latest, basis_version):
            from .domain import ConflictError
            raise ConflictError("当前版本已存在通过会签")
        signoff = self.repository.create_signoff(item_id, conclusion, basis_version, actor)
        self.repository.append_audit("signoff_submit", ENTITY, item_id, actor, {
            "signoff_id": signoff["id"], "basis_version": basis_version,
        })
        return signoff

    def review_signoff(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, roles_for_signoff("review"))
        actor = require_text(actor, "actor", 100)
        decision = validate_signoff_decision(payload.get("decision"))
        comment = payload.get("comment")
        if comment is not None:
            comment = require_text(comment, "comment")
        if decision == "rejected" and not comment:
            from .domain import ValidationError
            raise ValidationError("退回会签必须填写退回意见")
        item = self.repository.get_item(item_id)
        if not can_review_signoff(item["status"]):
            from .domain import ConflictError
            raise ConflictError("仅处于医学随访的事件可以复核会签")
        signoff = self.repository.latest_signoff(item_id)
        if signoff is None or signoff["status"] != "submitted":
            from .domain import ConflictError
            raise ConflictError("当前没有待复核的会签")
        if signoff["basis_version"] != item["version"]:
            from .domain import ConflictError
            raise ConflictError("会签依据版本已过期，需在新版本上重新提交")
        if not different_reviewer(signoff["submitted_by"], actor):
            from .domain import PermissionDenied
            raise PermissionDenied("自己提交的会签不能由本人复核")
        reviewed = self.repository.review_signoff(signoff["id"], decision, comment, actor)
        self.repository.append_audit("signoff_review", ENTITY, item_id, actor, {
            "signoff_id": reviewed["id"], "decision": decision,
            "basis_version": reviewed["basis_version"], "comment": comment,
        })
        return reviewed

    def list_signoffs(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_signoffs(item_id)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        return self.enrich(item, self.repository.latest_signoff(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = self.repository.list_items(status)
        signoffs = self.repository.latest_signoffs([item["id"] for item in items])
        return [self.enrich(item, signoffs.get(item["id"])) for item in items]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def _signoff_summary(signoff: Optional[Dict[str, Any]], current_version: int,
                         item_status: Optional[str] = None) -> dict:
        if not signoff:
            return {"required": True, "status": "missing", "current": False}
        # 关闭动作消费当版通过会签：对已关闭事件它始终是有效关闭依据
        consumed = item_status == "closed" and signoff["status"] == "approved"
        current = consumed or signoff_is_current(signoff, current_version)
        reason = None
        if signoff["status"] == "invalidated":
            reason = signoff.get("invalidated_reason")
        elif signoff["status"] == "rejected":
            reason = signoff.get("review_comment")
        elif signoff["status"] == "approved" and not current:
            reason = "依据版本已过期"
        return {
            "required": True,
            "status": signoff["status"],
            "current": current,
            "basis_version": signoff["basis_version"],
            "conclusion": signoff["conclusion"],
            "submitted_by": signoff["submitted_by"],
            "submitted_at": signoff["submitted_at"],
            "reviewed_by": signoff.get("reviewed_by"),
            "reviewed_at": signoff.get("reviewed_at"),
            "review_comment": signoff.get("review_comment"),
            "invalidated_reason": reason,
            "invalidated_at": signoff.get("invalidated_at"),
        }

    @staticmethod
    def enrich(item: Dict[str, Any], signoff: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["signoff"] = Service._signoff_summary(signoff, item["version"], item["status"])
        return result
