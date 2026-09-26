import tempfile
import unittest
from datetime import date
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.rules import DomainRules, shift_months


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
SERVICER = Actor("servicer-1", "servicer")


class RepaymentPlanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def activate(self, reference="MORT-PLAN-1"):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        record = self.service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "收入波动"})
        record = self.service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
        record = self.service.act(SERVICER, record["id"], record["version"], "activate", {"borrower_ack": True, "activation_date": "2026-01-15"})
        return record

    def view(self, record_id, as_of):
        return self.service.record_view(SERVICER, record_id, date.fromisoformat(as_of))

    def test_activation_generates_monthly_plan(self):
        record = self.activate()
        plan = record["payload"]["plan"]
        self.assertEqual(len(plan), 9)
        self.assertEqual(plan[0]["sequence"], 1)
        self.assertEqual(plan[0]["due_amount"], 3400.0)
        self.assertEqual(plan[0]["due_date"], "2026-02-15")
        self.assertEqual(plan[0]["paid_amount"], 0.0)
        self.assertEqual(plan[-1]["due_date"], "2026-10-15")
        self.assertEqual(record["payload"]["activation_date"], "2026-01-15")

    def test_shift_months_month_end(self):
        self.assertEqual(shift_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(shift_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(shift_months(date(2026, 1, 15), 3), date(2026, 4, 15))

    def test_register_payment_and_overdue_status(self):
        record = self.activate()
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 1, "amount": 3400.0, "paid_on": "2026-02-10"})
        self.assertEqual(record["payload"]["plan"][0]["paid_amount"], 3400.0)
        self.assertEqual(record["payload"]["plan"][0]["paid_on"], "2026-02-10")

        view = self.view(record["id"], "2026-03-16")
        status = view["plan_status"]
        by_seq = {i["sequence"]: i for i in status["installments"]}
        self.assertEqual(by_seq[1]["status"], "paid")
        self.assertEqual(by_seq[2]["status"], "overdue")
        self.assertEqual(by_seq[3]["status"], "pending")
        self.assertEqual(status["overdue_amount"], 3400.0)
        self.assertEqual(status["consecutive_overdue"], 1)
        self.assertEqual(status["outstanding"], 3400.0 * 8)

    def test_partial_payment_and_overpayment_rejected(self):
        record = self.activate()
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 1, "amount": 1000.0})
        view = self.view(record["id"], "2026-02-16")
        installment = view["plan_status"]["installments"][0]
        self.assertEqual(installment["status"], "overdue")
        self.assertEqual(installment["outstanding"], 2400.0)
        self.assertEqual(view["plan_status"]["overdue_amount"], 2400.0)
        with self.assertRaises(ValidationError):
            self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 1, "amount": 2500.0})
        with self.assertRaises(ValidationError):
            self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 1, "amount": 0})

    def test_payment_requires_active_plan_and_servicer(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-EARLY", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 1, "amount": 100.0})
        record = self.activate("MORT-PERM")
        with self.assertRaises(PermissionDenied):
            self.service.register_payment(Actor("uw", "underwriter"), record["id"], record["version"], {"sequence": 1, "amount": 100.0})

    def test_two_consecutive_overdue_default_then_recover(self):
        record = self.activate()
        # 第1、2期到期未清 -> 连续两期逾期 -> 违约
        record = self.service.roll_status(SERVICER, record["id"], record["version"], date(2026, 3, 16))
        self.assertEqual(record["state"], "defaulted")
        # 幂等：再次滚动无变化
        again = self.service.roll_status(SERVICER, record["id"], record["version"], date(2026, 3, 16))
        self.assertEqual(again["version"], record["version"])

        # 只补齐第1期，第2期仍逾期 -> 维持违约
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 1, "amount": 3400.0, "paid_on": "2026-03-20"})
        self.assertEqual(record["state"], "defaulted")
        # 补齐第2期 -> 欠缴清零，自动恢复履约中
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 2, "amount": 3400.0, "paid_on": "2026-03-25"})
        self.assertEqual(record["state"], "active")
        view = self.view(record["id"], "2026-03-26")
        self.assertEqual(view["plan_status"]["overdue_amount"], 0.0)
        self.assertEqual(view["plan_status"]["consecutive_overdue"], 0)

    def test_gap_in_overdue_does_not_default(self):
        record = self.activate()
        # 只缴第2期：第1、3期逾期但不连续 -> 不违约
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 2, "amount": 3400.0, "paid_on": "2026-03-01"})
        record = self.service.roll_status(SERVICER, record["id"], record["version"], date(2026, 4, 16))
        self.assertEqual(record["state"], "active")
        view = self.view(record["id"], "2026-04-16")
        self.assertEqual(view["plan_status"]["consecutive_overdue"], 1)
        self.assertEqual(view["plan_status"]["overdue_amount"], 3400.0 * 2)

    def test_audit_trail_records_default_and_recover(self):
        record = self.activate()
        record = self.service.roll_status(SERVICER, record["id"], record["version"], date(2026, 3, 16))
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 1, "amount": 3400.0, "paid_on": "2026-03-20"})
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"sequence": 2, "amount": 3400.0, "paid_on": "2026-03-20"})
        actions = [e["action"] for e in self.service.timeline(SERVICER, record["id"])]
        self.assertIn("default", actions)
        self.assertIn("payment", actions)
        self.assertIn("recover", actions)


if __name__ == "__main__":
    unittest.main()
