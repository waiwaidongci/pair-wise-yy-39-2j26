# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、读数分级/合并判定和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制、批次/读数/任务表和审计链。
- `src/service.py`：权限检查、批次编排、缺陷/任务生成、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/batches`：登记巡检批次（inspector），一批携带多个坝段读数
- `GET /api/batches`：批次列表，可用`?queue=priority`或`?queue=review`筛选
- `GET /api/batches/{id}`：批次详情（读数、优先计数、关联任务）
- `GET /api/tasks`、`GET /api/items/{id}/tasks`：处置任务
- `POST /api/tasks/{id}/claim`：由对应责任角色接手任务
- `GET /api/audit`

### 巡检批次

批次载荷：`external_ref`（批次编号）、可选`note`、可选`reported_at`、可选
`assignees`（角色→责任人姓名覆盖），以及`readings`数组，每条读数含
`section`（坝段）、`metric`（`seepage`渗流/`displacement`位移/`crack`裂缝）、
`value`、`control_value`（控制值）、可选`external_ref`与`reported_at`。

- 读数超过控制值即进入优先队列（`state=priority`），自动生成缺陷（初始为
  `inspected`）和处置任务；按超出倍数分级`minor/major/emergency`，
  分别由值班坝工工程师或值班应急经理负责，到场时限按等级与超限倍数计算。
- 同一`external_ref`按最早报告时间合并到同一缺陷：一致读数记`merged`并累计
  连续异常次数；后到的矛盾读数（超限状态翻转或数值/控制值偏差超过10%）
  记`review`进入待复核，不产生新缺陷。
- 批次产生的缺陷必须有关闭状态的`reinspection`复检记录才能关闭。

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
