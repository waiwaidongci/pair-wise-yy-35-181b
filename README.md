# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/signoffs`：会签历史
- `POST /api/items/{id}/signoffs`：辐射防护员提交关闭会签（`basis_version`依据版本、`conclusion`随访结论）
- `POST /api/items/{id}/signoffs/{sid}/review`：卫生物理师审核（`decision=approved|rejected`，退回必填`comment`）
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 关闭会签

事件进入医学随访（`follow_up`）后，关闭前必须完成会签，而不再只检查未结记录：

- 辐射防护员（radiation_officer）提交随访结论和依据的数据版本`basis_version`，依据版本必须是当前版本。
- 卫生物理师（health_physicist）审核，且必须换人：提交人不能审核自己的会签；退回（rejected）必须填写意见。
- 事件状态变动或随访记录变动后，原有会签一律失效（invalidated）并记录失效原因（`status_changed`/`records_changed`），须在新版本数据上重新提交、重新审核；退回意见同样要在数据更新后重新处理。
- 只有当前版本、未失效且已通过（approved）的会签才允许关闭事件；关闭时在同一数据库事务内复核，防并发绕过。
- 事件详情（`GET /api/items/{id}`）内嵌最新会签：状态、提交人/审核人、审核意见、失效原因及其中文说明、依据版本是否仍为当前版本。
- 规则判定在`src/rules.py`，存储与版本失效在`src/repository.py`，用例编排在`src/service.py`，JSON路由在`src/http_api.py`，各司其职。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
