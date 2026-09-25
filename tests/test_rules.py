import unittest
from src import rules
from src.domain import ConflictError, ValidationError
class RulesTest(unittest.TestCase):
    def test_priority_deadline_and_escalation(self):
        low=rules.priority_score(rules.SEVERITIES[0],1,10,0); high=rules.priority_score(rules.SEVERITIES[-1],30,10,3)
        self.assertGreater(high,low); self.assertLessEqual(rules.response_deadline_hours(rules.SEVERITIES[-1],30,10),rules.response_deadline_hours(rules.SEVERITIES[0],1,10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[-1],1,10)); self.assertTrue(rules.escalation_required(rules.SEVERITIES[0],10,10))
    def test_transition_guards(self):
        self.assertTrue(rules.can_transition(rules.STATES[0],rules.STATES[1]))
        with self.assertRaises(ConflictError): rules.validate_transition(rules.STATES[0],rules.STATES[-1])
        with self.assertRaises(ValidationError): rules.priority_score("not-a-severity",1,1)
    def test_batch_breach_merge_and_deadline_rules(self):
        # 超过控制值即入队；分级随倍数升高
        self.assertFalse(rules.exceeds_control(20,20)); self.assertTrue(rules.exceeds_control(20.1,20))
        self.assertEqual(rules.breach_severity(21,20),'minor')
        self.assertEqual(rules.breach_severity(30,20),'major')
        self.assertEqual(rules.breach_severity(45,20),'emergency')
        # 紧急级别到场时限更短
        self.assertLessEqual(rules.arrival_deadline_hours('emergency',45,20),
                             rules.arrival_deadline_hours('minor',21,20))
        # 矛盾容差10%
        self.assertFalse(rules.reading_contradicts(31,31.2))
        self.assertTrue(rules.reading_contradicts(80,31))
        self.assertTrue(rules.reading_contradicts(1,0))
        # 默认责任人按指标分派
        self.assertIn("seepage", rules.default_assignee("seepage"))
        # 关闭不变量：缺复检、有未关闭事项
        self.assertEqual(rules.close_blockers(0,False,False),[])
        self.assertIn("复检", "；".join(rules.close_blockers(0,True,False)))
        self.assertIn("未关闭", "；".join(rules.close_blockers(1,True,True)))
        self.assertEqual(rules.close_blockers(0,True,True),[])
        # 复检要求与应急处置判定一致
        self.assertTrue(rules.reinspection_required(
            {"severity":"major","quantity":20,"threshold":20}))
        self.assertFalse(rules.reinspection_required(
            {"severity":"major","quantity":5,"threshold":10}))
if __name__=="__main__": unittest.main()
