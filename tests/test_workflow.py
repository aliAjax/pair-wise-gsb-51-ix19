import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import add_months, parse_due_date


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
SERVICER = Actor("operator", "servicer")


def activate(service, reference, first_due=None, borrower_id=None):
    """跑通 提交→评估→批准→生效 全流程，返回生效后的记录。"""
    data = dict(CREATE_DATA)
    if borrower_id:
        data["borrower_id"] = borrower_id
    record = service.create(Actor("creator", "intake_officer"), reference, data)
    record = service.act(Actor("operator", "intake_officer"), record["id"], record["version"], "assess", {'assessment_note': '收入波动'})
    record = service.act(Actor("operator", "underwriter"), record["id"], record["version"], "approve", {'exception_approved': False})
    action_data = {'borrower_ack': True}
    if first_due:
        action_data['first_due_date'] = first_due
    return service.act(SERVICER, record["id"], record["version"], "activate", action_data)


def first_due_with_overdue(count):
    """返回一个首期到期日，使当前恰好有 count 期到期（到期日早于今天）。"""
    # 每期约30天，第 count 期到期日放在3天前，保证第 count+1 期在未来
    return date.today() - timedelta(days=count * 30 - 3)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_activate_generates_monthly_plan(self):
        first_due = "2026-04-10"
        record = activate(self.service, "MORT-27001", first_due)
        self.assertEqual(record["state"], "active")
        self.assertEqual(len(record["payload"]["schedule"]), 9)
        plan = self.service.payment_plan(SERVICER, record["id"])
        rows = plan["installments"]
        self.assertEqual(plan["approved_months"], 9)
        self.assertEqual([row["installment"] for row in rows], list(range(1, 10)))
        self.assertEqual([row["due_date"] for row in rows[:3]], ["2026-04-10", "2026-05-10", "2026-06-10"])
        self.assertEqual(rows[-1]["due_date"], "2026-12-10")
        self.assertTrue(all(row["due_amount"] == plan["approved_payment"] for row in rows))
        self.assertEqual(plan["summary"]["total_due"], round(plan["approved_payment"] * 9, 2))

    def test_overdue_default_recovery_and_completion(self):
        # 三期已到期：前两期连续逾期 → 违约；补齐两期恢复，再全部结清 → 完成
        first_due = first_due_with_overdue(2).isoformat()
        record = activate(self.service, "MORT-27002", first_due)

        plan = self.service.payment_plan(SERVICER, record["id"])
        self.assertEqual(plan["summary"]["overdue_count"], 2)
        self.assertGreaterEqual(plan["summary"]["consecutive_overdue"], 2)

        record = self.service.act(SERVICER, record["id"], record["version"], "evaluate", {})
        self.assertEqual(record["state"], "defaulted")

        due = plan["approved_payment"]
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"installment": 1, "amount": due})
        self.assertEqual(record["state"], "defaulted")  # 还有一期逾期
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"installment": 2, "amount": due})
        self.assertEqual(record["state"], "active")

        plan = self.service.payment_plan(SERVICER, record["id"])
        self.assertEqual(plan["summary"]["overdue_amount"], 0.0)
        self.assertTrue(all(row["status"] != "overdue" for row in plan["installments"]))

        for installment in range(3, 10):
            record = self.service.register_payment(
                SERVICER, record["id"], record["version"], {"installment": installment, "amount": due}
            )
        self.assertEqual(record["state"], "completed")
        self.assertEqual(self.service.payment_plan(SERVICER, record["id"])["summary"]["outstanding"], 0.0)

    def test_single_overdue_does_not_default(self):
        first_due = first_due_with_overdue(1).isoformat()
        record = activate(self.service, "MORT-27003", first_due)
        plan = self.service.payment_plan(SERVICER, record["id"])
        self.assertEqual(plan["summary"]["overdue_count"], 1)
        self.assertEqual(plan["summary"]["consecutive_overdue"], 1)
        record = self.service.act(SERVICER, record["id"], record["version"], "evaluate", {})
        self.assertEqual(record["state"], "active")

    def test_cure_blocked_until_arrears_cleared(self):
        first_due = first_due_with_overdue(2).isoformat()
        record = activate(self.service, "MORT-27004", first_due)
        record = self.service.act(SERVICER, record["id"], record["version"], "evaluate", {})
        self.assertEqual(record["state"], "defaulted")
        with self.assertRaises(ValidationError):
            self.service.act(SERVICER, record["id"], record["version"], "cure", {})

    def test_partial_payment_keeps_overdue(self):
        first_due = first_due_with_overdue(2).isoformat()
        record = activate(self.service, "MORT-27005", first_due)
        due = record["payload"]["approved_payment"]
        record = self.service.act(SERVICER, record["id"], record["version"], "evaluate", {})
        self.assertEqual(record["state"], "defaulted")
        record = self.service.register_payment(SERVICER, record["id"], record["version"], {"installment": 1, "amount": due / 2})
        self.assertEqual(record["state"], "defaulted")
        plan = self.service.payment_plan(SERVICER, record["id"])
        self.assertEqual(plan["installments"][0]["status"], "overdue")
        self.assertAlmostEqual(plan["installments"][0]["outstanding"], round(due - due / 2, 2), places=2)

    def test_payment_records_audit(self):
        first_due = add_months(date.today(), 1).replace(day=10).isoformat()
        record = activate(self.service, "MORT-27006", first_due)
        due = record["payload"]["approved_payment"]
        record = self.service.register_payment(
            SERVICER, record["id"], record["version"], {"installment": 1, "amount": due}
        )
        timeline = self.service.timeline(SERVICER, record["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions, ["created", "assess", "approve", "activate", "payment"])
        self.assertEqual(timeline[-1]["details"]["installment"], 1)
        self.assertEqual(timeline[-1]["details"]["amount"], due)

    def test_payment_rejects_bad_installment_and_state(self):
        # approved 但未生效，不能登记收款
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27007", CREATE_DATA)
        record = self.service.act(Actor("operator", "intake_officer"), record["id"], record["version"], "assess", {'assessment_note': 'x'})
        record = self.service.act(Actor("operator", "underwriter"), record["id"], record["version"], "approve", {'exception_approved': False})
        with self.assertRaises(ValidationError):
            self.service.register_payment(SERVICER, record["id"], record["version"], {"installment": 1, "amount": 100})

        record = activate(self.service, "MORT-27008", add_months(date.today(), 1).isoformat(), borrower_id="B-008")
        with self.assertRaises(ValidationError):
            self.service.register_payment(SERVICER, record["id"], record["version"], {"installment": 99, "amount": 100})
        with self.assertRaises(PermissionDenied):
            self.service.register_payment(Actor("x", "underwriter"), record["id"], record["version"], {"installment": 1, "amount": 100})

    def test_plan_not_available_before_activation(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27009", CREATE_DATA)
        with self.assertRaises(ValidationError):
            self.service.payment_plan(SERVICER, record["id"])

    def test_activate_defaults_first_due_to_next_month(self):
        record = activate(self.service, "MORT-27010")
        first_due = parse_due_date(record["payload"]["first_due_date"])
        self.assertEqual(first_due, add_months(date.today(), 1))
