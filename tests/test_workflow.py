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
        current=self.service.get_item(item["id"],"viewer")
        for target in STATES[1:-1]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"],'follow_up')
        # 辐射防护员提交随访结论，卫生物理师换人复核后才能关闭
        self.service.submit_signoff(current["id"],{"conclusion":"随访剂量恢复正常"},"rp-officer",'radiation_officer')
        current=self.service.get_item(current["id"],"viewer")
        self.assertEqual(current["signoff"]["status"],"submitted")
        self.service.review_signoff(current["id"],{"decision":"approve","comment":"结论与数据一致"},"hp-reviewer",'health_physicist')
        current=self.service.get_item(current["id"],"viewer")
        self.assertTrue(current["signoff"]["current"])
        current=self.service.transition(current["id"],'closed',current["version"],"reviewer",TRANSITION_ROLES['closed'][0])
        self.assertEqual(current["status"],STATES[-1])
        # 关闭后详情仍展示被消费的通过会签、审核人与依据版本
        self.assertEqual(current["signoff"]["status"],"approved")
        self.assertTrue(current["signoff"]["current"])
        self.assertEqual(current["signoff"]["reviewed_by"],"hp-reviewer")
        self.assertEqual(current["signoff"]["basis_version"],current["version"]-1)
        self.assertEqual(len(self.service.list_records(current["id"],"viewer")),1)
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(STATES)+3); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
