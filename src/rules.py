"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, date_field, integer, number, text, text_list

# 贷款生命周期：
# submitted -> assessed -> approved -> active（方案生效，生成按月还款计划）
# active/defaulted 之间由每期履约情况驱动：连续两期逾期进入违约，欠缴补齐恢复。
INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {
    'assess': {'intake_officer'},
    'approve': {'underwriter'},
    'activate': {'servicer'},
    'cure': {'servicer'},
    'default': {'servicer'},
    'payment': {'servicer'},
    'roll': {'servicer'},
}
TRANSITIONS = {
    'assess': {'submitted': 'assessed'},
    'approve': {'assessed': 'approved'},
    'activate': {'approved': 'active'},
    'cure': {'active': 'cured'},
    'default': {'active': 'defaulted'},
}

# 每期状态（由应还/实收/到期日推导）
INSTALLMENT_PENDING = "pending"      # 未到期
INSTALLMENT_PAID = "paid"            # 已缴清
INSTALLMENT_PARTIAL = "partial"      # 有实收但未缴清（未到期）
INSTALLMENT_OVERDUE = "overdue"      # 到期未清（部分或未付）
DEFAULT_AFTER_CONSECUTIVE_OVERDUE = 2


def shift_months(day: date, months: int) -> date:
    """按月推进，月末（如31日）在短月回落到该月最后一天。"""
    total = (day.year * 12 + day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last_day))


def _money(value: float) -> float:
    return round(float(value) + 1e-9, 2)


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
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    # ---- 还款计划 ----

    def build_plan(self, payload: Dict[str, Any], start: date) -> List[Dict[str, Any]]:
        """方案生效时生成按月还款计划：批准月供、批准月数、按月到期日。"""
        months = int(payload["approved_months"])
        payment = _money(payload["approved_payment"])
        plan: List[Dict[str, Any]] = []
        for index in range(1, months + 1):
            due = shift_months(start, index)
            plan.append({
                "sequence": index,
                "due_amount": payment,
                "due_date": due.isoformat(),
                "paid_amount": 0.0,
                "paid_on": None,
            })
        return plan

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
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
            start = date_field(data, "activation_date", date.today())
            changes["borrower_ack"] = True
            changes["activation_date"] = start.isoformat()
            changes["plan"] = self.build_plan(p, start)
            summary = "纾困方案生效，生成%s期按月还款计划" % int(p["approved_months"])
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 收款登记 ----

    def register_payment(self, record: Dict[str, Any], data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """把一笔实收登记到指定期次，返回新payload与审计细节。不改变贷款状态（状态由履约推导）。"""
        if record["state"] not in {"active", "defaulted"}:
            raise Conflict("方案生效后才能登记收款")
        p = dict(record["payload"])
        plan = p.get("plan")
        if not isinstance(plan, list) or not plan:
            raise Conflict("还款计划尚未生成")
        sequence = integer(data, "sequence", 1, len(plan))
        amount = number(data, "amount", 0.01)
        paid_on = date_field(data, "paid_on", date.today())

        installment = dict(plan[sequence - 1])
        due_amount = float(installment["due_amount"])
        already = float(installment["paid_amount"])
        if _money(already) >= _money(due_amount):
            raise Conflict("第%s期已缴清" % sequence)
        if _money(already + amount) > _money(due_amount):
            raise ValidationError("实收超过第%s期应还余额" % sequence)

        new_paid = _money(already + amount)
        installment["paid_amount"] = new_paid
        installment["paid_on"] = paid_on.isoformat()
        plan[sequence - 1] = installment
        p["plan"] = plan
        details = {
            "sequence": sequence,
            "amount": _money(amount),
            "paid_on": paid_on.isoformat(),
            "due_amount": _money(due_amount),
            "outstanding": _money(due_amount - new_paid),
        }
        return p, details

    # ---- 履约状态推导 ----

    @staticmethod
    def installment_status(installment: Dict[str, Any], as_of: date) -> str:
        paid = _money(installment.get("paid_amount", 0.0))
        due_amount = _money(installment["due_amount"])
        if paid >= due_amount:
            return INSTALLMENT_PAID
        due = date.fromisoformat(installment["due_date"])
        if as_of > due:
            return INSTALLMENT_OVERDUE
        return INSTALLMENT_PENDING if paid == 0.0 else INSTALLMENT_PARTIAL

    def plan_status(self, record: Dict[str, Any], as_of: Optional[date] = None) -> Optional[Dict[str, Any]]:
        """推导每期状态、当前欠缴和连续逾期情况。"""
        as_of = as_of or date.today()
        plan = record.get("payload", {}).get("plan")
        if not isinstance(plan, list) or not plan:
            return None
        installments: List[Dict[str, Any]] = []
        overdue_amount = 0.0
        outstanding = 0.0
        consecutive = 0
        max_consecutive = 0
        paid_count = 0
        for item in plan:
            status = self.installment_status(item, as_of)
            due_amount = _money(item["due_amount"])
            remaining = _money(due_amount - float(item.get("paid_amount", 0.0)))
            if status == INSTALLMENT_PAID:
                paid_count += 1
                consecutive = 0
            else:
                outstanding += remaining
                if status == INSTALLMENT_OVERDUE:
                    overdue_amount += remaining
                    consecutive += 1
                    max_consecutive = max(max_consecutive, consecutive)
                else:
                    consecutive = 0
            installments.append({
                "sequence": item["sequence"],
                "due_amount": due_amount,
                "due_date": item["due_date"],
                "paid_amount": _money(item.get("paid_amount", 0.0)),
                "paid_on": item.get("paid_on"),
                "outstanding": remaining,
                "status": status,
            })
        return {
            "as_of": as_of.isoformat(),
            "installments": installments,
            "total_due": _money(sum(float(i["due_amount"]) for i in plan)),
            "total_paid": _money(sum(float(i.get("paid_amount", 0.0)) for i in plan)),
            "outstanding": _money(outstanding),
            "overdue_amount": _money(overdue_amount),
            "consecutive_overdue": max_consecutive,
            "paid_count": paid_count,
            "total_count": len(plan),
        }

    def roll_target(self, record: Dict[str, Any], as_of: Optional[date] = None) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        """
        根据履约推导贷款应处状态：
        - 连续两期到期未清 -> defaulted（违约）
        - 违约后欠缴全部补齐 -> active（恢复）
        无变化时返回None。
        """
        status = self.plan_status(record, as_of)
        if status is None:
            return None
        state = record["state"]
        if state == "active" and status["consecutive_overdue"] >= DEFAULT_AFTER_CONSECUTIVE_OVERDUE:
            return ("defaulted", "default", {
                "summary": "连续两期逾期，贷款进入违约",
                "auto": True,
                "as_of": status["as_of"],
                "consecutive_overdue": status["consecutive_overdue"],
                "overdue_amount": status["overdue_amount"],
            })
        if state == "defaulted" and _money(status["overdue_amount"]) == 0.0:
            return ("active", "recover", {
                "summary": "欠缴已补齐，贷款恢复正常",
                "auto": True,
                "as_of": status["as_of"],
            })
        return None
