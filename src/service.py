"""业务用例编排、权限检查与审计。"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


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

    def record_view(self, actor: Actor, record_id: int, as_of: Optional[date] = None) -> Dict[str, Any]:
        """记录详情视图：附带每期状态、当前欠缴等履约推导结果。"""
        record = self.get_record(actor, record_id)
        status = self.rules.plan_status(record, as_of)
        if status is not None:
            record["plan_status"] = status
        return record

    def register_payment(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """登记某期实收；违约贷款欠缴补齐后自动恢复正常。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "payment"):
            raise PermissionDenied("角色无权登记收款")
        record = self.repository.get(record_id)
        new_payload, details = self.rules.register_payment(record, data or {})
        # 收款后的履约状态以收款日期为基准（支持补登历史收款）
        eval_date = datetime.strptime(details["paid_on"], "%Y-%m-%d").date()
        target = self.rules.roll_target({"state": record["state"], "payload": new_payload}, eval_date)
        new_state = record["state"]
        roll_event = None
        if target is not None:
            new_state, roll_action, roll_details = target
            details["loan_state"] = {"from": record["state"], "to": new_state, "reason": roll_details["summary"]}
            roll_event = (roll_action, roll_details)
        saved = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action="payment",
            details=details,
        )
        # 状态因收款而改变（违约恢复等）时，追加一条独立审计事件
        if roll_event is not None:
            self.audit.note(record_id, actor.user_id, roll_event[0], roll_event[1])
        return saved

    def roll_status(self, actor: Actor, record_id: int, expected_version: int, as_of: Optional[date] = None) -> Dict[str, Any]:
        """按履约情况滚动贷款状态：连续两期逾期进入违约，欠缴补齐恢复。幂等。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "roll"):
            raise PermissionDenied("角色无权执行状态滚动")
        record = self.repository.get(record_id)
        target = self.rules.roll_target(record, as_of)
        if target is None:
            return record
        new_state, audit_action, details = target
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=record["payload"],
            actor_id=actor.user_id,
            action=audit_action,
            details=details,
        )

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
