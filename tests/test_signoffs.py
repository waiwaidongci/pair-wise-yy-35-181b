import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class SignoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        item = self.service.create_item(
            {"title": "signoff item", "description": "close countersign",
             "severity": "high", "quantity": 1, "threshold": 1, "external_ref": "SO-1"},
            "creator", "dosimetrist")
        # 记录均关闭，避免干扰关闭闸门
        self.service.add_record(item["id"], {"kind": "evidence", "detail": "d",
                                             "status": "closed", "external_ref": "R-1"},
                                "recorder", "radiation_officer")
        current = self.service.get_item(item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", TRANSITION_ROLES[target][0])
        self.item_id = current["id"]

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _approve(self, basis_item=None):
        self.service.submit_signoff(self.item_id, {"conclusion": "随访无异常"},
                                    "rp", "radiation_officer")
        return self.service.review_signoff(self.item_id, {"decision": "approve"},
                                           "hp", "health_physicist")

    def test_record_change_invalidates_approved_signoff(self):
        signoff = self._approve()
        self.assertEqual(signoff["status"], "approved")
        # 记录变动 -> 新版本，原通过会签失效
        self.service.add_record(self.item_id, {"kind": "note", "detail": "补充材料",
                                               "status": "closed", "external_ref": "R-2"},
                                "rp", "radiation_officer")
        detail = self.service.get_item(self.item_id, "viewer")
        self.assertEqual(detail["signoff"]["status"], "invalidated")
        self.assertEqual(detail["signoff"]["invalidated_reason"], "记录变动")
        self.assertFalse(detail["signoff"]["current"])
        with self.assertRaises(ConflictError):
            self.service.transition(self.item_id, "closed", detail["version"],
                                    "hp", "health_physicist")
        # 新版本重新会签后方可关闭
        self._approve()
        detail = self.service.get_item(self.item_id, "viewer")
        closed = self.service.transition(self.item_id, "closed", detail["version"],
                                         "hp", "health_physicist")
        self.assertEqual(closed["status"], "closed")

    def test_rejected_must_be_resubmitted_on_new_version(self):
        self.service.submit_signoff(self.item_id, {"conclusion": "初版结论"},
                                    "rp", "radiation_officer")
        rejected = self.service.review_signoff(
            self.item_id, {"decision": "reject", "comment": "依据不足，补记录"},
            "hp", "health_physicist")
        self.assertEqual(rejected["status"], "rejected")
        # 同一版本不能重新提交
        with self.assertRaises(ConflictError):
            self.service.submit_signoff(self.item_id, {"conclusion": "再次提交"},
                                        "rp", "radiation_officer")
        # 依据数据变动产生新版本后才能重新处理退回意见（退回意见作为历史保留）
        self.service.add_record(self.item_id, {"kind": "note", "detail": "补充记录",
                                               "status": "closed", "external_ref": "R-3"},
                                "rp", "radiation_officer")
        # add_record 产生了新版本，事件仍处于 follow_up（记录变动不改状态）
        detail = self.service.get_item(self.item_id, "viewer")
        self.assertEqual(detail["status"], "follow_up")
        self.assertEqual(detail["signoff"]["status"], "rejected")
        self.assertFalse(detail["signoff"]["current"])
        self.service.submit_signoff(self.item_id, {"conclusion": "补充后结论"},
                                    "rp", "radiation_officer")
        approved = self.service.review_signoff(
            self.item_id, {"decision": "approve"}, "hp2", "health_physicist")
        self.assertEqual(approved["status"], "approved")

    def test_only_health_physicist_can_review(self):
        self.service.submit_signoff(self.item_id, {"conclusion": "x"},
                                    "rp", "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.review_signoff(self.item_id, {"decision": "approve"},
                                        "rp", "radiation_officer")

    def test_submitted_signoff_invalidated_by_record_change_blocks_review(self):
        self.service.submit_signoff(self.item_id, {"conclusion": "待审"},
                                    "rp", "radiation_officer")
        self.service.add_record(self.item_id, {"kind": "note", "detail": "新记录",
                                               "status": "closed", "external_ref": "R-4"},
                                "rp2", "radiation_officer")
        with self.assertRaises(ConflictError):
            self.service.review_signoff(self.item_id, {"decision": "approve"},
                                        "hp", "health_physicist")

    def test_signoff_history_records_each_round(self):
        self.service.submit_signoff(self.item_id, {"conclusion": "v1"},
                                    "rp", "radiation_officer")
        self.service.review_signoff(self.item_id, {"decision": "reject", "comment": "退回"},
                                    "hp", "health_physicist")
        self.service.add_record(self.item_id, {"kind": "note", "detail": "补",
                                               "status": "closed", "external_ref": "R-5"},
                                "rp", "radiation_officer")
        self.service.submit_signoff(self.item_id, {"conclusion": "v2"},
                                    "rp", "radiation_officer")
        self.service.review_signoff(self.item_id, {"decision": "approve"},
                                    "hp", "health_physicist")
        history = self.service.list_signoffs(self.item_id, "viewer")
        self.assertEqual([s["status"] for s in history], ["rejected", "approved"])
        self.assertEqual(history[0]["reviewed_by"], "hp")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
