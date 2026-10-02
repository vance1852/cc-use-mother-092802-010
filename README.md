# 建立跨工厂质量对标与整改闭环基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/quality_benchmark/`：跨工厂质量对标平台（版本化指标、周期冻结、独立审查、整改闭环）；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 跨工厂质量对标平台

`quality_benchmark` 在基础服务之上实现全球质量排名的完整闭环：

- **版本化指标**：指标定义、适用产品、采样窗口、检测方法版本与可比性调整（产品组合系数、产线改造系数、方法偏移）随版本固化；方法换版产生新版本并记录 supersede 链，周期创建时锁定具体版本；
- **周期冻结**：冻结时把各工厂提交的证据与计算结果固化为分数快照，计算过程（公式、批次、系数、输入哈希）一并留痕；
- **异常值排除**：工厂按批次申请排除，必须由独立于该工厂组织的审查人审批；批准后重算分数，并在申请记录中保留排除前后的指标名次与总分名次影响；
- **异议处理**：存在未决异议的指标在发布时自动暂停（已发布的立即挂起该指标最新版本），其余指标照常发布；异议驳回后可重新发布，异议成立则冻结该分数；
- **整改闭环**：整改计划关联具体发现（指标结果/异议/排除记录）、责任人、期限与复验证据，由独立审查人验证；完成整改不改写任何已发布排名；
- **追溯与权限分离**：`ranking.view`、`ranking.trace`、`batch.release` 三种授权互相独立；管理层可经 `GET /quality/trace?purpose=ranking` 从任一分数追溯到原始批次与计算规则，排名数据明确不能作为产品批次放行依据，放行决定只依据独立放行材料。

### 主要接口（均以 `/quality` 为前缀）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/quality/metric-versions`、`/quality/metric-versions/activate` | 定义指标版本、激活（自动退役旧版） |
| POST | `/quality/cycles`、`/quality/cycles/freeze`、`/quality/cycles/publish` | 创建周期（锁定指标版本）、冻结、发布 |
| POST | `/quality/submissions` | 工厂提交证据（批次、产品、检测值、采样日期） |
| POST | `/quality/exclusions`、`/quality/exclusions/review` | 异常值排除申请与独立审查 |
| POST | `/quality/disputes`、`/quality/disputes/resolve` | 工厂异议与独立裁决 |
| POST | `/quality/plans`、`/quality/plans/evidence`、`/quality/plans/verify` | 整改计划、复验证据与验证 |
| POST | `/quality/grants`、`/quality/release-decisions` | 授权管理、批次放行决定 |
| GET | `/quality/rankings`、`/quality/scorecards`、`/quality/trace` | 排名发布视图、工厂分数卡、分数追溯 |
| GET | `/quality/metric-versions`、`/quality/cycles`、`/quality/exclusions`、`/quality/disputes`、`/quality/plans`、`/quality/release-decisions` | 版本、周期与流程记录查询 |

### 平台离线验收

```bash
PYTHONPATH=src python3 -m quality_benchmark.acceptance
```

验收在临时数据库中走完整业务链（方法换版 → 冻结 → 排除审查 → 异议暂停 → 发布 → 整改验证 → 权限分离追溯），成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

### 平台 HTTP 服务

```bash
PYTHONPATH=src python3 -m quality_benchmark.api --database quality.sqlite3 --host 127.0.0.1 --port 8080
```

`/quality` 前缀之外的请求（建档、健康检查、审计查询）自动回退到基础服务路由，两个服务共享同一 SQLite 数据库与审计链。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
