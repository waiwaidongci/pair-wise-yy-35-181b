import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_complete_workflow_and_audit(self):
        item=self.service.create_item({"title":"workflow item","description":"complete business flow","severity":'high',"quantity":12,"threshold":6,"external_ref":"WF-1"},"creator",'dosimetrist')
        self.assertEqual(item["status"],STATES[0])
        self.service.add_record(item["id"],{"kind":"evidence","detail":"evidence registered","status":"closed","external_ref":"EV-1"},"recorder",'radiation_officer')
        current=item
        for target in STATES[1:-1]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"],'follow_up')
        signoff=self.service.submit_signoff(current["id"],{"basis_version":current["version"],"conclusion":"随访结论：指标恢复正常","evidence_ref":"FU-1"},"rpo-alice",'radiation_officer')
        self.assertEqual(signoff["status"],'submitted'); self.assertIsNone(signoff["reviewed_by"])
        approved=self.service.review_signoff(current["id"],signoff["id"],{"decision":"approved","comment":"同意关闭"},"hp-bob",'health_physicist')
        self.assertEqual(approved["status"],'approved'); self.assertEqual(approved["reviewed_by"],"hp-bob")
        current=self.service.transition(current["id"],STATES[-1],current["version"],"hp-bob",TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(current["status"],STATES[-1])
        self.assertEqual(current["signoff"]["status"],'approved'); self.assertEqual(current["signoff"]["reviewed_by"],"hp-bob")
        self.assertEqual(len(self.service.list_records(current["id"],"viewer")),1)
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(STATES)+3); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
