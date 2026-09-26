import unittest
from datetime import date

from src.domain import ValidationError
from src.rules import (
    DomainRules,
    INSTALLMENT_OVERDUE,
    INSTALLMENT_PAID,
    INSTALLMENT_PENDING,
    add_months,
)


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["disposable_income"], 9000.0)
        self.assertEqual(prepared["eligible_months"], 9)
        self.assertTrue(prepared["housing_ratio"] < 0.4)

    def test_assess_eligibility(self):
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, "assess", {'assessment_note': '收入波动'}, today=date(2026, 3, 1))
        self.assertEqual(state, "assessed")
        self.assertTrue(payload["eligibility"])

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["program_type"] = 'pause'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

    def test_build_schedule_is_monthly(self):
        schedule = self.rules.build_schedule(3400.0, 6, date(2026, 4, 10))
        self.assertEqual(len(schedule), 6)
        self.assertEqual([item["installment"] for item in schedule], [1, 2, 3, 4, 5, 6])
        self.assertEqual([item["due_date"] for item in schedule],
                         ["2026-04-10", "2026-05-10", "2026-06-10", "2026-07-10", "2026-08-10", "2026-09-10"])
        self.assertTrue(all(item["due_amount"] == 3400.0 for item in schedule))
        self.assertTrue(all(item["paid_amount"] == 0.0 for item in schedule))

    def test_schedule_clamps_month_end(self):
        # 1月31日 + 1个月应为2月最后一天（2026非闰年 → 28日）
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        # 闰月
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))

    def test_installment_status_by_due_date_and_payment(self):
        schedule = self.rules.build_schedule(1000.0, 4, date(2026, 1, 15))
        today = date(2026, 3, 20)
        # 第1期全额、第2期部分、第3期未付、第4期未到期
        self.rules.apply_payment({"schedule": schedule}, 1, 1000.0, "svc", "2026-02-10T00:00:00+00:00")
        self.rules.apply_payment({"schedule": schedule}, 2, 400.0, "svc", "2026-03-10T00:00:00+00:00")
        view = self.rules.schedule_view({"schedule": schedule}, today)
        rows = view["installments"]
        self.assertEqual(rows[0]["status"], INSTALLMENT_PAID)
        self.assertEqual(rows[1]["status"], INSTALLMENT_OVERDUE)
        self.assertEqual(rows[1]["outstanding"], 600.0)
        self.assertEqual(rows[2]["status"], INSTALLMENT_OVERDUE)
        self.assertEqual(rows[3]["status"], INSTALLMENT_PENDING)
        summary = view["summary"]
        self.assertEqual(summary["overdue_count"], 2)
        self.assertEqual(summary["overdue_amount"], 1600.0)
        self.assertEqual(summary["consecutive_overdue"], 2)
        self.assertEqual(summary["outstanding"], 2600.0)
        self.assertFalse(summary["all_paid"])

    def test_evaluate_default_only_after_two_consecutive(self):
        schedule = self.rules.build_schedule(1000.0, 4, date(2026, 1, 15))
        # 只逾期第1期（第2期已付），3月20日时第3期刚逾期，但中间第2期已付，最大连续逾期只有1
        self.rules.apply_payment({"schedule": schedule}, 2, 1000.0, "svc", "2026-03-01T00:00:00+00:00")
        payload = {"schedule": schedule}
        state, reason = self.rules.evaluate_state("active", payload, date(2026, 3, 20))
        self.assertEqual(state, "active")
        self.assertEqual(reason, "")
        # 4月20日第3期也逾期：逾期的是第1、3期，中间隔着已付的第2期，仍不构成连续两期
        # （第4期04-15也到期，为避免3、4期相邻连续，提前付清第4期）
        self.rules.apply_payment({"schedule": schedule}, 4, 1000.0, "svc", "2026-04-01T00:00:00+00:00")
        state, _ = self.rules.evaluate_state("active", payload, date(2026, 4, 20))
        self.assertEqual(state, "active")

    def test_evaluate_default_and_recovery(self):
        schedule = self.rules.build_schedule(1000.0, 4, date(2026, 2, 20))
        payload = {"schedule": schedule}
        # 3月20日：第1期（02-20）、第2期（03-20尚未到期）→ 只有1期逾期，不违约
        state, _ = self.rules.evaluate_state("active", payload, date(2026, 3, 20))
        self.assertEqual(state, "active")
        # 4月20日：第1、2期均逾期 → 违约
        state, reason = self.rules.evaluate_state("active", payload, date(2026, 4, 20))
        self.assertEqual(state, "defaulted")
        self.assertEqual(reason, "consecutive_overdue")
        # 补齐第1、2、3期（第3期04-20在21日也已逾期1天）；第4期05-20未到期，无欠缴 → 恢复
        self.rules.apply_payment(payload, 1, 1000.0, "svc", "2026-04-21T00:00:00+00:00")
        self.rules.apply_payment(payload, 2, 1000.0, "svc", "2026-04-21T00:00:00+00:00")
        self.rules.apply_payment(payload, 3, 1000.0, "svc", "2026-04-21T00:00:00+00:00")
        state, reason = self.rules.evaluate_state("defaulted", payload, date(2026, 4, 21))
        self.assertEqual(state, "active")
        self.assertEqual(reason, "")

    def test_evaluate_completed_when_all_paid(self):
        schedule = self.rules.build_schedule(1000.0, 2, date(2026, 1, 15))
        payload = {"schedule": schedule}
        for installment in (1, 2):
            self.rules.apply_payment(payload, installment, 1000.0, "svc", "2026-03-01T00:00:00+00:00")
        state, reason = self.rules.evaluate_state("active", payload, date(2026, 3, 20))
        self.assertEqual(state, "completed")
        self.assertEqual(reason, "all_paid")

    def test_payment_over_installment_range_rejected(self):
        schedule = self.rules.build_schedule(1000.0, 2, date(2026, 1, 15))
        with self.assertRaises(ValidationError):
            self.rules.apply_payment({"schedule": schedule}, 3, 1000.0, "svc", "now")
        with self.assertRaises(ValidationError):
            self.rules.apply_payment({"schedule": schedule}, 1, 0, "svc", "now")
