"""业务用例编排、权限检查与审计。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, integer, number, text
from .repository import Repository
from .rules import DEFAULT_REASON, DomainRules


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        if action == "evaluate" and new_state == record["state"]:
            return record
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def payment_plan(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        if not record["payload"].get("schedule"):
            raise ValidationError("方案尚未生效，暂无还款计划")
        view = self.rules.schedule_view(record["payload"])
        return {
            "record_id": record["id"],
            "reference": record["reference"],
            "state": record["state"],
            "version": record["version"],
            "approved_payment": record["payload"].get("approved_payment"),
            "approved_months": record["payload"].get("approved_months"),
            "first_due_date": record["payload"].get("first_due_date"),
            "plan_effective_date": record["payload"].get("plan_effective_date"),
            **view,
        }

    def register_payment(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "payment"):
            raise PermissionDenied("角色无权登记收款")
        data = data or {}
        installment_no = integer(data, "installment", 1)
        amount = number(data, "amount", 0.01)
        record = self.repository.get(record_id)
        if record["state"] not in {"active", "defaulted"}:
            raise ValidationError("仅生效中的还款方案可以登记收款")

        payload = dict(record["payload"])
        payload["schedule"] = [dict(item, payments=list(item.get("payments", []))) for item in (payload.get("schedule") or [])]
        before = self.rules.schedule_view(payload)["summary"]
        received_at = _now()
        self.rules.apply_payment(payload, installment_no, amount, actor.user_id, received_at)

        target_state, reason = self.rules.evaluate_state(record["state"], payload)
        if target_state == "defaulted":
            payload["default_reason"] = DEFAULT_REASON
        elif target_state == "active" and record["state"] == "defaulted":
            payload["recovered_at"] = received_at
        elif target_state == "completed":
            payload["completed_at"] = received_at

        after = self.rules.schedule_view(payload)["summary"]
        details = {
            "summary": "第%s期登记实收%.2f元" % (installment_no, float(amount)),
            "installment": installment_no,
            "amount": round(float(amount), 2),
            "received_at": received_at,
            "from": record["state"],
            "to": target_state,
            "state_change_reason": reason,
            "overdue_before": before["overdue_count"],
            "overdue_after": after["overdue_count"],
        }
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=target_state,
            payload=payload,
            actor_id=actor.user_id,
            action="payment",
            details=details,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
