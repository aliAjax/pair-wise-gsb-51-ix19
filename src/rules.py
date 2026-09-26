"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
import calendar
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {
    'assess': {'intake_officer'},
    'approve': {'underwriter'},
    'activate': {'servicer'},
    'evaluate': {'servicer'},
    'cure': {'servicer'},
    'default': {'servicer'},
    'payment': {'servicer'},
}
TRANSITIONS = {
    'assess': {'submitted': 'assessed'},
    'approve': {'assessed': 'approved'},
    'activate': {'approved': 'active'},
    'cure': {'defaulted': 'active'},
    'default': {'active': 'defaulted'},
}
EVALUATION_STATES = {'active', 'defaulted'}
PLAN_STATES = {'active', 'defaulted'}
COMPLETED_STATE = 'completed'

INSTALLMENT_PENDING = 'pending'
INSTALLMENT_OVERDUE = 'overdue'
INSTALLMENT_PAID = 'paid'

DEFAULT_REASON = '连续两期到期未清'


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def parse_due_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("到期日必须是YYYY-MM-DD格式") from exc


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def money(value: Any) -> float:
    return round(float(value), 2)


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        if action == "evaluate":
            if record["state"] not in EVALUATION_STATES:
                raise Conflict("仅生效中的还款方案可以评估履约状态")
            return record["state"]
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    # ---- 还款计划 ----

    @staticmethod
    def build_schedule(monthly_payment: float, months: int, first_due: date) -> List[Dict[str, Any]]:
        due_amount = money(monthly_payment)
        return [
            {
                "installment": index,
                "due_amount": due_amount,
                "due_date": add_months(first_due, index - 1).isoformat(),
                "paid_amount": 0.0,
                "payments": [],
            }
            for index in range(1, int(months) + 1)
        ]

    @staticmethod
    def installment_view(installment: Dict[str, Any], today: date) -> Dict[str, Any]:
        due_amount = money(installment["due_amount"])
        paid_amount = money(installment["paid_amount"])
        outstanding = money(max(due_amount - paid_amount, 0.0))
        if outstanding <= 0:
            status = INSTALLMENT_PAID
        elif parse_due_date(installment["due_date"]) < today:
            status = INSTALLMENT_OVERDUE
        else:
            status = INSTALLMENT_PENDING
        return {
            "installment": int(installment["installment"]),
            "due_amount": due_amount,
            "due_date": installment["due_date"],
            "paid_amount": paid_amount,
            "outstanding": outstanding,
            "status": status,
            "payments": list(installment.get("payments", [])),
        }

    def schedule_view(self, payload: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
        today = today or utc_today()
        schedule = payload.get("schedule") or []
        rows: List[Dict[str, Any]] = []
        overdue_count = 0
        overdue_amount = 0.0
        total_due = 0.0
        total_paid = 0.0
        outstanding_total = 0.0
        consecutive = 0
        consecutive_overdue = 0
        paid_count = 0
        for item in schedule:
            row = self.installment_view(item, today)
            rows.append(row)
            total_due += row["due_amount"]
            total_paid += row["paid_amount"]
            outstanding_total += row["outstanding"]
            if row["status"] == INSTALLMENT_PAID:
                paid_count += 1
                consecutive = 0
            elif row["status"] == INSTALLMENT_OVERDUE:
                overdue_count += 1
                overdue_amount += row["outstanding"]
                consecutive += 1
                consecutive_overdue = max(consecutive_overdue, consecutive)
            else:
                consecutive = 0
        installment_count = len(schedule)
        all_paid = installment_count > 0 and paid_count == installment_count
        return {
            "installments": rows,
            "summary": {
                "installment_count": installment_count,
                "paid_count": paid_count,
                "overdue_count": overdue_count,
                "consecutive_overdue": consecutive_overdue,
                "total_due": money(total_due),
                "total_paid": money(total_paid),
                "outstanding": money(outstanding_total),
                "overdue_amount": money(overdue_amount),
                "all_paid": all_paid,
            },
        }

    def apply_payment(
        self,
        payload: Dict[str, Any],
        installment_no: int,
        amount: float,
        actor_id: str,
        received_at: str,
    ) -> None:
        schedule = payload.get("schedule")
        if not schedule:
            raise Conflict("方案尚未生效，无法登记收款")
        if not 1 <= installment_no <= len(schedule):
            raise ValidationError("期次超出还款计划范围（共%s期）" % len(schedule))
        if amount <= 0:
            raise ValidationError("实收金额必须大于0")
        item = schedule[installment_no - 1]
        item["paid_amount"] = money(float(item["paid_amount"]) + amount)
        item.setdefault("payments", []).append(
            {"amount": money(amount), "received_at": received_at, "actor_id": actor_id}
        )

    def evaluate_state(self, state: str, payload: Dict[str, Any], today: Optional[date] = None) -> Tuple[str, str]:
        """按当前日期评估贷款级状态，返回(新状态, 原因)；状态不变时原因为空串。"""
        if not payload.get("schedule"):
            return state, ""
        view = self.schedule_view(payload, today)
        summary = view["summary"]
        if state == "defaulted" and summary["overdue_amount"] <= 0:
            state = "active"
        if state == "active":
            if summary["consecutive_overdue"] >= 2:
                return "defaulted", "consecutive_overdue"
            if summary["all_paid"]:
                return COMPLETED_STATE, "all_paid"
        return state, ""

    # ---- 动作 ----

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], today: Optional[date] = None) -> Tuple[str, Dict[str, Any], str]:
        today = today or utc_today()
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            exception = boolean(data, "exception_approved")
            if not p.get("eligibility") and not exception:
                raise ValidationError("不符合纾困资格且无例外批准")
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["exception_approved"] = exception
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            first_due_text = optional_text(data, "first_due_date")
            first_due = parse_due_date(first_due_text) if first_due_text else add_months(today, 1)
            months = int(p["approved_months"])
            changes["borrower_ack"] = True
            changes["plan_effective_date"] = today.isoformat()
            changes["first_due_date"] = first_due.isoformat()
            changes["schedule"] = self.build_schedule(p["approved_payment"], months, first_due)
            summary = "纾困方案生效，生成%s期按月还款计划" % months
        elif action == "cure":
            view = self.schedule_view(p, today)
            if view["summary"]["overdue_amount"] > 0:
                raise ValidationError("逾期欠缴尚未补齐，不能恢复正常")
            changes["recovered_at"] = today.isoformat()
            summary = "欠缴补齐，贷款恢复正常"
        elif action == "default":
            view = self.schedule_view(p, today)
            if view["summary"]["consecutive_overdue"] < 2:
                raise ValidationError("尚未达到连续两期逾期的违约条件")
            reason = optional_text(data, "default_reason", DEFAULT_REASON)
            changes["default_reason"] = reason or DEFAULT_REASON
            summary = "连续两期逾期，贷款进入违约"
        elif action == "evaluate":
            target_state, reason = self.evaluate_state(record["state"], p, today)
            if target_state == record["state"]:
                return record["state"], p, "履约状态无变化"
            if target_state == "defaulted":
                changes["default_reason"] = DEFAULT_REASON
                summary = "连续两期逾期，贷款进入违约"
            elif target_state == "active":
                changes["recovered_at"] = today.isoformat()
                summary = "欠缴补齐，贷款恢复正常"
            elif target_state == COMPLETED_STATE:
                changes["completed_at"] = today.isoformat()
                summary = "所有期次结清，还款计划完成"
            new_state = target_state
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
