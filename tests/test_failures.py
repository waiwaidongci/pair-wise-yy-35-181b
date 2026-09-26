import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"failure item","description":"failure scenarios","severity":'high',"quantity":5,"threshold":10,"external_ref":"FAIL-1"},"creator",'dosimetrist')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _to_follow_up(self, item):
        current=item
        for target in STATES[1:-1]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        return current
    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied): self.service.transition(self.item["id"],STATES[1],1,"attacker","viewer")
        with self.assertRaises(ConflictError): self.service.transition(self.item["id"],STATES[1],99,"reviewer",TRANSITION_ROLES[STATES[1]][0])
        payload={"kind":"action","detail":"same reference","status":"open","external_ref":"DUP-1"}
        self.service.add_record(self.item["id"],payload,"recorder",'radiation_officer')
        with self.assertRaises(ConflictError): self.service.add_record(self.item["id"],payload,"recorder",'radiation_officer')
        current=self.service.get_item(self.item["id"],"viewer")
        current=self._to_follow_up(current)
        # 既有未关闭记录 + 缺少会签，都会阻止关闭
        with self.assertRaises(ConflictError): self.service.transition(current["id"],STATES[-1],current["version"],"reviewer",TRANSITION_ROLES[STATES[-1]][0])
    def test_close_requires_current_approved_signoff(self):
        current=self._to_follow_up(self.service.get_item(self.item["id"],"viewer"))
        self.service.submit_signoff(current["id"],{"conclusion":"随访完成"},"rp-officer",'radiation_officer')
        current=self.service.get_item(self.item["id"],"viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"],'closed',current["version"],"hp",'health_physicist')
        self.service.review_signoff(current["id"],{"decision":"approve"},"hp",'health_physicist')
        current=self.service.get_item(self.item["id"],"viewer")
        closed=self.service.transition(current["id"],'closed',current["version"],"hp",'health_physicist')
        self.assertEqual(closed["status"],'closed')
    def test_self_review_forbidden(self):
        current=self._to_follow_up(self.service.get_item(self.item["id"],"viewer"))
        self.service.submit_signoff(current["id"],{"conclusion":"同一人提交"},"same-person",'radiation_officer')
        with self.assertRaises(PermissionDenied):
            self.service.review_signoff(current["id"],{"decision":"approve"},"same-person",'health_physicist')
    def test_submit_requires_radiation_officer(self):
        current=self._to_follow_up(self.service.get_item(self.item["id"],"viewer"))
        with self.assertRaises(PermissionDenied):
            self.service.submit_signoff(current["id"],{"conclusion":"越权提交"},"x",'health_physicist')
    def test_reject_requires_comment(self):
        current=self._to_follow_up(self.service.get_item(self.item["id"],"viewer"))
        self.service.submit_signoff(current["id"],{"conclusion":"待核"},"rp",'radiation_officer')
        with self.assertRaises(ValidationError):
            self.service.review_signoff(current["id"],{"decision":"reject"},"hp",'health_physicist')
if __name__=="__main__": unittest.main()
