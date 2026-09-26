import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class SignoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        item = self.service.create_item(
            {"title": "signoff item", "description": "closing countersign",
             "severity": "high", "quantity": 12, "threshold": 6,
             "external_ref": "SIGN-1"},
            "creator", "dosimetrist")
        self.service.add_record(
            item["id"], {"kind": "evidence", "detail": "registered",
                         "status": "closed", "external_ref": "EV-1"},
            "recorder", "radiation_officer")
        current = item
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        self.item_id = current["id"]
        self.version = current["version"]

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _submit(self, actor="rpo-alice", version=None):
        return self.service.submit_signoff(
            self.item_id,
            {"basis_version": self.version if version is None else version,
             "conclusion": "随访结论：剂量恢复正常", "evidence_ref": "FU-1"},
            actor, "radiation_officer")

    def test_close_blocked_without_current_approved_signoff(self):
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.item_id, "closed", self.version,
                "hp-bob", "health_physicist")
        submitted = self._submit()
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.item_id, "closed", self.version,
                "hp-bob", "health_physicist")
        self.service.review_signoff(
            self.item_id, submitted["id"], {"decision": "approved"},
            "hp-bob", "health_physicist")
        closed = self.service.transition(
            self.item_id, "closed", self.version, "hp-bob", "health_physicist")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["signoff"]["status"], "approved")
        self.assertTrue(closed["signoff"]["basis_current"])
        # 关闭后的补充记录不应使关闭时采用的会签失效
        _, invalidated = self.repo.add_record(
            self.item_id, "follow_up", "关闭后归档补充", "closed", "EV-POST", "rec")
        self.assertEqual(invalidated, [])
        after = self.service.get_item(self.item_id, "viewer")
        self.assertEqual(after["signoff"]["status"], "approved")
        self.assertTrue(after["signoff"]["active"])

    def test_submitter_cannot_review_own_signoff(self):
        submitted = self._submit(actor="rpo-alice")
        with self.assertRaises(PermissionDenied):
            self.service.review_signoff(
                self.item_id, submitted["id"], {"decision": "approved"},
                "rpo-alice", "health_physicist")
        with self.assertRaises(PermissionDenied):
            self.service.review_signoff(
                self.item_id, submitted["id"], {"decision": "approved"},
                "hp-bob", "radiation_officer")

    def test_record_change_invalidates_approved_signoff(self):
        submitted = self._submit()
        self.service.review_signoff(
            self.item_id, submitted["id"], {"decision": "approved"},
            "hp-bob", "health_physicist")
        record, invalidated = self.repo.add_record(
            self.item_id, "follow_up", "新的随访记录", "closed", "EV-2",
            "recorder2")
        self.assertEqual(invalidated, [submitted["id"]])
        detail = self.service.get_item(self.item_id, "viewer")
        self.assertIsNone(detail["signoff"])
        history = self.service.list_signoffs(self.item_id, "viewer")
        stale = next(s for s in history if s["id"] == submitted["id"])
        self.assertEqual(stale["status"], "invalidated")
        self.assertEqual(stale["invalidated_reason"], "records_changed")
        self.assertEqual(stale["invalidated_reason_label"], "随访记录已变动")
        self.assertFalse(stale["active"])
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.item_id, "closed", detail["version"],
                "hp-bob", "health_physicist")
        resubmitted = self.service.submit_signoff(
            self.item_id,
            {"basis_version": detail["version"], "conclusion": "补充材料后结论不变"},
            "rpo-alice", "radiation_officer")
        self.service.review_signoff(
            self.item_id, resubmitted["id"], {"decision": "approved"},
            "hp-carol", "health_physicist")
        closed = self.service.transition(
            self.item_id, "closed", detail["version"],
            "hp-carol", "health_physicist")
        self.assertEqual(closed["status"], "closed")

    def test_rejection_requires_comment_and_must_be_reworked_on_new_data(self):
        submitted = self._submit()
        with self.assertRaises(ValidationError):
            self.service.review_signoff(
                self.item_id, submitted["id"], {"decision": "rejected"},
                "hp-bob", "health_physicist")
        rejected = self.service.review_signoff(
            self.item_id, submitted["id"],
            {"decision": "rejected", "comment": "缺少尿检复核材料"},
            "hp-bob", "health_physicist")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reviewed_by"], "hp-bob")
        with self.assertRaises(ConflictError):
            self._submit()
        self.repo.add_record(
            self.item_id, "follow_up", "补交尿检复核", "closed", "EV-3",
            "recorder3")
        item = self.service.get_item(self.item_id, "viewer")
        new = self.service.submit_signoff(
            self.item_id,
            {"basis_version": item["version"], "conclusion": "已补交复核材料"},
            "rpo-alice", "radiation_officer")
        self.assertEqual(new["status"], "submitted")
        self.assertTrue(item["signoff"] is None or item["signoff"]["status"] != "submitted")

    def test_submission_guards(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_signoff(
                self.item_id, {"basis_version": self.version, "conclusion": "x"},
                "dosimetrist-dave", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.submit_signoff(
                self.item_id, {"basis_version": self.version + 99,
                               "conclusion": "版本过期"},
                "rpo-alice", "radiation_officer")
        self._submit()
        with self.assertRaises(ConflictError):
            self._submit()

    def test_invalidated_signoff_cannot_be_reviewed(self):
        submitted = self._submit()
        self.repo.add_record(
            self.item_id, "follow_up", "数据变动", "closed", "EV-4", "rec")
        with self.assertRaises(ConflictError):
            self.service.review_signoff(
                self.item_id, submitted["id"], {"decision": "approved"},
                "hp-bob", "health_physicist")


if __name__ == "__main__":
    unittest.main()
