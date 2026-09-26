import tempfile
import unittest
from datetime import date
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.rules import add_months


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
SERVICER = Actor("operator", "servicer")


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create_and_assess(self, reference="MORT-27001"):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        return self.service.act(Actor("operator", "intake_officer"), record["id"], record["version"], "assess", {'assessment_note': '收入波动'})

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "MORT-27001", CREATE_DATA)
        self.service.create(Actor("creator", "intake_officer"), "MORT-27001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "intake_officer"), "MORT-27001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self._create_and_assess()
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", "underwriter"), record["id"], record["version"] - 1, "approve", {'exception_approved': False})

    def test_wrong_role_cannot_approve(self):
        record = self._create_and_assess()
        with self.assertRaises(PermissionDenied):
            self.service.act(SERVICER, record["id"], record["version"], "approve", {'exception_approved': False})

    def test_approve_without_eligibility_rejected(self):
        data = dict(CREATE_DATA)
        data['arrears'] = 50000.0  # 欠缴超过月供的6倍(42000)，不符合资格
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27002", data)
        record = self.service.act(Actor("operator", "intake_officer"), record["id"], record["version"], "assess", {'assessment_note': 'x'})
        self.assertFalse(record["payload"]["eligibility"])
        with self.assertRaises(ValidationError):
            self.service.act(Actor("operator", "underwriter"), record["id"], record["version"], "approve", {'exception_approved': False})

    def test_activate_without_ack_rejected(self):
        record = self._create_and_assess("MORT-27003")
        record = self.service.act(Actor("operator", "underwriter"), record["id"], record["version"], "approve", {'exception_approved': False})
        with self.assertRaises(ValidationError):
            self.service.act(SERVICER, record["id"], record["version"], "activate", {'borrower_ack': False})

    def test_manual_default_requires_two_consecutive_overdue(self):
        record = self._create_and_assess("MORT-27004")
        record = self.service.act(Actor("operator", "underwriter"), record["id"], record["version"], "approve", {'exception_approved': False})
        record = self.service.act(SERVICER, record["id"], record["version"], "activate", {'borrower_ack': True})
        # 计划首期还在未来，手动标记违约应被拒绝
        with self.assertRaises(ValidationError):
            self.service.act(SERVICER, record["id"], record["version"], "default", {'default_reason': '人为判定'})

    def test_action_in_wrong_state_rejected(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27005", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.act(SERVICER, record["id"], record["version"], "activate", {'borrower_ack': True})

    def test_duplicate_activation_rejected(self):
        record = self._create_and_assess("MORT-27006")
        record = self.service.act(Actor("operator", "underwriter"), record["id"], record["version"], "approve", {'exception_approved': False})
        record = self.service.act(SERVICER, record["id"], record["version"], "activate", {'borrower_ack': True, 'first_due_date': add_months(date.today(), 1).isoformat()})
        with self.assertRaises(Conflict):
            self.service.act(SERVICER, record["id"], record["version"], "activate", {'borrower_ack': True})
