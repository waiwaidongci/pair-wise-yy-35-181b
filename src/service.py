from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_decision, require_number, require_text,
                     require_version)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    SIGNOFF_SUBMIT_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    signoff_close_blocker, signoff_summary,
                    validate_signoff_review, validate_signoff_submission,
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
        record, invalidated = self.repository.add_record(
            item_id, kind, detail, status, external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        if invalidated:
            self.repository.append_audit("signoff_invalidated", ENTITY, item_id, actor, {
                "signoff_ids": invalidated, "reason": "records_changed",
                "trigger": "record", "record_id": record["id"],
            })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        expected_version = require_version(expected_version, "expected_version")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        blockers += signoff_close_blocker(
            item, self.repository.latest_active_signoff(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated, invalidated = self.repository.transition_item(
            item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        if invalidated:
            self.repository.append_audit("signoff_invalidated", ENTITY, item_id, actor, {
                "signoff_ids": invalidated, "reason": "status_changed",
                "trigger": "transition", "to": target,
            })
        return self.enrich(updated)

    def submit_signoff(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SIGNOFF_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        basis_version = require_version(payload.get("basis_version"), "basis_version")
        conclusion = require_text(payload.get("conclusion"), "conclusion")
        evidence_ref = payload.get("evidence_ref")
        if evidence_ref is not None:
            evidence_ref = require_text(evidence_ref, "evidence_ref", 200)
        active = self.repository.latest_active_signoff(item_id)
        validate_signoff_submission(item, basis_version, active)
        signoff = self.repository.create_signoff(
            item_id, basis_version, conclusion, evidence_ref, actor)
        self.repository.append_audit("signoff_submit", ENTITY, item_id, actor, {
            "signoff_id": signoff["id"], "basis_version": basis_version,
        })
        return self._signoff_view(signoff, item["version"])

    def review_signoff(self, item_id: int, signoff_id: int,
                       payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        signoff = self.repository.get_signoff(signoff_id)
        if signoff["item_id"] != item_id:
            raise NotFoundError("会签不属于该事件")
        decision = require_decision(payload.get("decision"))
        comment = payload.get("comment")
        if comment is not None:
            comment = require_text(comment, "comment")
        if decision == "rejected" and not comment:
            from .domain import ValidationError
            raise ValidationError("退回会签必须填写退回意见")
        validate_signoff_review(signoff, role, actor)
        reviewed = self.repository.review_signoff(signoff_id, decision, comment, actor)
        self.repository.append_audit("signoff_review", ENTITY, item_id, actor, {
            "signoff_id": signoff_id, "decision": decision,
            "submitted_by": signoff["submitted_by"],
        })
        return self._signoff_view(reviewed, item["version"])

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def list_signoffs(self, item_id: int, role: str) -> list:
        self._view(role)
        item = self.repository.get_item(item_id)
        return [self._signoff_view(s, item["version"])
                for s in self.repository.list_signoffs(item_id)]

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def _signoff_view(self, signoff: Dict[str, Any],
                      current_version: int) -> Dict[str, Any]:
        return signoff_summary(signoff, current_version)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        signoff = self.repository.latest_active_signoff(item["id"])
        closing = (item["status"] == "closed"
                   and signoff is not None
                   and signoff["id"] == item.get("closed_signoff_id"))
        result["signoff"] = signoff_summary(signoff, item["version"], closing)
        return result
