# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

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
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`，支持 `?status=`、`?queue=priority|review`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/batches`：巡检批次一次携带多个坝段读数
- `GET /api/batches?queue=priority|review`：批次列表，可按优先队列/待复核筛选
- `GET /api/batches/{id}`
- `POST /api/readings/{id}/review`：`{"action":"confirm|dismiss"}`处理待复核读数
- `GET /api/tasks[?status=open|accepted|done]`
- `POST /api/tasks/{id}/accept`：接手处置任务
- `GET /api/audit`

### 巡检批次规则

- 一批携带多个坝段读数（`dam_section` + `metric`：seepage渗流/displacement位移/crack裂缝 + `value`读数 + `control_value`控制值 + 可选`external_ref`、`reported_at`）。
- 读数超过控制值即进入优先队列，自动创建缺陷并生成处置任务：按指标分派责任人（值班工程师），按超限严重程度给出到场时限（minor/major/emergency分别24/8/4小时起，超限越多时限越短）。
- 重复`external_ref`按最早`reported_at`合并到同一缺陷，不重复建单、不重复派任务；后到读数与最早读数相对差超过10%视为矛盾，进入待复核，确认后再补派任务，否定则解除超限标记。
- 应急处置缺陷（emergency级别或读数达到控制值）必须有`kind=reinspection`且`status=closed`的复检记录才能关闭；仍有未关闭事项同样不能关闭。

### 分层职责

- `src/domain.py`：批次/读数/任务的数据结构、枚举与输入校验。
- `src/rules.py`：超限判定与分级、矛盾容差、责任人分派、到场时限、复检关闭不变量、角色矩阵。
- `src/repository.py`：`batches`/`readings`/`tasks`表、索引、事务与审计链存储。
- `src/service.py`：批次接收编排（合并、待复核、建缺陷、派任务）、复核、接手、筛选。
- `src/http_api.py`：批次、复核、任务路由与统一错误响应。

允许角色：inspector, dam_engineer, emergency_manager, viewer。批次登记限inspector；读数复核限inspector/dam_engineer；任务接手限dam_engineer/emergency_manager。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
