# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：演示页面，含一键演示流程、还款计划、当前欠缴、每期状态和收款登记。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}?as_of=YYYY-MM-DD`：记录详情，生效记录附带`plan_status`（每期状态、当前欠缴、连续逾期等推导结果）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  - `activate` 的 data 支持可选 `activation_date`（默认今天）；方案生效时按批准月供与批准月数生成按月还款计划，首期为生效次月同日。
- `POST /api/records/{id}/payments`：登记某期实收，data 为 `{"sequence":1,"amount":3400.00,"paid_on":"2026-02-10"}`（`paid_on` 默认今天）。支持部分实收，超额拒绝；违约贷款欠缴补齐后自动恢复并追加审计事件。
- `POST /api/records/{id}/roll`：按 `as_of`（默认今天）核对履约状态并滚动：连续两期到期未清进入违约，违约后欠缴清零恢复正常；幂等，无变化时不产生新版本。

### 还款计划与履约状态

- 每期记录：期次 `sequence`、应还额 `due_amount`（批准月供）、到期日 `due_date`、累计实收 `paid_amount`、最近收款日 `paid_on`。
- 每期状态（按核对基准日推导）：`pending` 未到期 / `partial` 部分实收未到期 / `paid` 已缴清 / `overdue` 到期未清。
- `plan_status` 汇总：当前欠缴 `overdue_amount`（到期未清合计）、计划剩余 `outstanding`、连续逾期期数 `consecutive_overdue`、已缴清期数。
- 违约与恢复：`active` 下连续两期逾期 → `defaulted`；`defaulted` 下欠缴补齐 → 自动回到 `active`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及还款计划生成、部分/超额收款、逾期标记、连续两期违约、欠缴补齐恢复和审计事件。
