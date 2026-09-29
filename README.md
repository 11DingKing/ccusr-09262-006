# 国际职教合作成效核算

面向主管单位的纯服务端系统：登记指标定义、数据版本、口径换算规则与证据来源，
在指定观察期内生成**可复算**的项目结论。全部代码仅依赖 Python 3.11 标准库，
持久化为 SQLite，接口为 WSGI JSON API。

## 核心规则

- **指标定义版本化**：定义更新只新增版本（v1、v2…）。报告固化版本号，
  旧报告永不被改写，且始终能按固化版本复算。
- **数据只追加、迟到数据形成新版本**：观测按自然键
  `(项目, 度量, 期间, 口径)` 记录；每批导入递增数据版本并给出
  `added / changed / retracted` 差异。缺失值显式登记为 `null`。
- **口径会签留痕**：换算规则（`canonical = value × factor + offset`）
  需指定多方会签，集齐签署方后恰好生效一次；规则回滚只切换生效指针，
  历史版本保留，已出报告不受影响。
- **可复算结论**：每份报告固化 `pins`（数据版本、指标版本、规则版本）、
  输入指纹与结果指纹（规范化 JSON 的 SHA-256）。复算严格按固化版本取数。
- **授权粒度**：机构仅能访问被授权的 `项目 × 指标类别 × 权限`；
  主管单位（`X-Role: supervisor`）拥有全部范围。
- **已签报告修订**：复核通过即首次签发（修订 v1）。更正绝不原地改写，
  只生成新修订（v2、v3…）；申请与审批均做 Python 层版本校验
  （`expected_version` 乐观锁），版本过期返回明确 409 冲突（含
  `expected/current`）。每个签发版本的完整行快照与更正理由保留在
  SQLite 签发历史中，读者可同时读取旧签发内容与更正理由；审批人不得是
  更正申请人。旧库连接时自动把已签发报告补登为 v1。
- **幂等与断点恢复**：计算任务以幂等键去重；计算分
  `snapshot → convert → aggregate → persist` 四步，每步落检查点，
  失败/崩溃后再次执行从断点续跑，重复执行收敛为同一份报告。
- **运行数据不进源码目录**：数据库路径通过 `APP_DB_PATH` 或 `--db` 注入，
  缺省使用系统临时目录。

## 分层结构

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 领域模型 | `service_09252_010/domain/` | 指标/版本/规则/报告、期间窗口、公式求值、换算、指纹 |
| 应用服务 | `service_09252_010/services/` | 指标登记、导入、会签、计算、复核、导出、授权 |
| 持久化 | `service_09252_010/persistence/` | SQLite 模式、工作单元、仓储 |
| 接口边界 | `service_09252_010/interfaces/wsgi_app.py` | WSGI 路由与 JSON 错误映射 |
| 端口 | `service_09252_010/ports.py` | 可替换的时钟与标识生成器（测试用确定性实现） |

## API 一览

所有请求带头 `X-Institution-Id: <机构>`；主管单位另带
`X-Role: supervisor`。写请求体为 JSON。

| 方法/路径 | 说明 |
| --- | --- |
| `POST /indicators` | 登记指标（含首个版本） |
| `GET  /indicators` / `GET /indicators/{code}` | 指标列表 / 详情（含全部版本） |
| `POST /indicators/{code}/versions` | 登记指标新版本 |
| `POST /evidence` | 登记证据来源（sha256、URI） |
| `POST /projects/{pid}/imports` | 导入数据，形成新数据版本并返回差异 |
| `GET  /projects/{pid}/versions/{ver}/diff?against=n` | 版本间差异 |
| `POST /rules` | 登记换算规则新版本（会签中） |
| `POST /rules/{rule_id}/signatures` | 会签（集齐自动生效） |
| `POST /rules/rollback` | 规则回滚到历史版本 |
| `GET  /rules` / `GET /rules/{rule_id}` | 规则列表 / 详情（含签署记录） |
| `POST /tasks` | 提交计算任务（头或体带 `idempotency_key`） |
| `POST /tasks/{task_id}/run` | 执行/断点恢复 |
| `GET  /tasks/{task_id}` | 任务状态、失败步骤与已完成断点 |
| `GET  /reports?project_id=...` | 报告列表（按授权类别过滤行） |
| `GET  /reports/{report_id}` | 报告详情（pins、指纹、事件、证据引用） |
| `POST /reports/{report_id}/reverify` | 按固化版本复算并核对指纹 |
| `POST /reports/{report_id}/review` | 复核通过/驳回（复核人不得是原计算人） |
| `POST /reports/{report_id}/revisions` | 已签报告更正申请（带 `expected_version` 乐观锁） |
| `POST /revisions/{revision_id}/decision` | 更正审批（审批人不得是申请人） |
| `GET  /reports/{report_id}/revisions` | 更正申请列表 |
| `GET  /reports/{report_id}/history` / `.../history/{ver}` | 签发历史 / 指定旧版本（含更正理由） |
| `POST /reports/{report_id}/exports` | 导出复核通过的报告（含换算依据与证据清单） |
| `POST /grants` | 主管单位配置机构授权 |

错误统一为 `{ "error": <码>, "message": ..., "detail": ... }`，
HTTP 状态：403 未授权、404 不存在、409 冲突、422 校验/缺失数据。

## 本地运行

```bash
python3 -m service_09252_010.serve --host 127.0.0.1 --port 8080 \
    --db /var/lib/service_09252_010/app.db
# 或
APP_DB_PATH=/var/lib/service_09252_010/app.db \
    python3 -m service_09252_010.serve
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖场景：缺失值三种策略（skip/zero/fail）、跨年度观察期窗口、
迟到数据新版本与差异、撤回记录、并发会签恰好生效一次、
幂等提交与并发收敛、断点恢复与失败标记、指标更新不可改写旧报告、
规则回滚仅影响新报告、授权粒度过滤、复核独立性与导出留痕、HTTP 全链路。
修订场景另覆盖：旧签发版本按号读取与新旧内容/理由并列、过期版本申请与
迟到申请审批的 409 冲突响应、驳回不产生新版本、旧库迁移补登 v1。

## 编译检查

```bash
python3 -m compileall -q service_09252_010 tests
```

扩展模块覆盖证据、审批、权限、留存、对账与恢复等业务边界。
