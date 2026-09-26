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
- `GET /api/items/{id}`，详情含`signoff`会签状态块
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/signoffs/submit`，辐射防护员提交随访结论与依据版本
- `POST /api/items/{id}/signoffs/review`，卫生物理师换人复核（`approve`/`reject`，退回须填`comment`）
- `GET /api/items/{id}/signoffs`，会签办理历史
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 关闭会签

事件进入医学随访（`follow_up`）后、关闭（`closed`）前必须完成关闭会签：

1. **提交**：仅辐射防护员（radiation_officer）可提交随访结论，系统自动锁定当前事件版本作为依据版本（`basis_version`）。
2. **复核**：仅卫生物理师（health_physicist）可复核，且不能是提交人本人——自己提的不能自己过关；退回必须填写意见。
3. **失效重办**：事件状态变动或记录新增都会产生新版本，原在途/已通过会签自动置为`invalidated`并记录失效原因（`记录变动`/`事件状态变动`）；被退回的会签必须等数据产生新版本后才能重新提交，退回意见在新版本上重新处理。
4. **关闭闸门**：只有当前版本上的通过会签（`approved`且依据版本等于当前版本、无未关闭事项）才允许关闭；关闭动作消费该会签。
5. **可追溯**：事件详情的`signoff`块展示会签状态、依据版本、提交人/审核人、退回意见和失效原因；`/signoffs`保留每一轮办理历史，提交、复核与失效均写入审计链。

分层职责：规则在`src/rules.py`（角色矩阵、失效判定、关闭不变量），存储在`src/repository.py`（`signoffs`表、版本联动失效，与版本推进同事务），接口在`src/service.py`/`src/http_api.py`（用例编排与JSON路由）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
