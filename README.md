# 建立跨工厂质量对标与整改闭环基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

在此基础上，项目内置**跨工厂质量对标平台**：版本化管理指标定义（适用产品、采样窗口、检测方法换版、可比性调整），按周期冻结各工厂提交的证据与计算结果；异常值排除必须由工厂以外的独立人员审查并保留排除前后名次影响；工厂异议只暂停争议指标、其他指标照常发布；整改计划关联发现、责任人、期限与复验证据，整改完成不改写已发布排名；批次放行权限与排名用途严格分离；任一分数均可回溯到原始批次与计算规则。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、离线验收与质量对标平台；
- `tests/`：基础规则、对标评分、冻结发布、异议整改、权限分离、接口路由和端到端验收测试。

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

基础服务：

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
```

质量对标平台（指标版本化 → 冻结 → 异议暂停 → 更正再发布 → 整改闭环 → 权限分离 → 分数追溯）：

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.benchmark_acceptance
```

成功时输出一行 `status` 为 `ok` 的 JSON（含逐项 `checks`）并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态、审计链与幂等回执继续保留。

## 质量对标平台接口

除健康检查外所有写接口都要求 `X-Actor-Id` 与唯一 `request_id`（重复提交按原响应幂等重放）。

### 角色

| 角色 | 用途 |
| --- | --- |
| `quality_manager` | 指标建档、周期管理、冻结发布、异议裁定 |
| `operator` | 本厂证据填报、异常值排除申请、提出/撤回异议、提交整改复验 |
| `reviewer` | 工厂以外独立人员：审查异常值排除、整改复验，登记发现 |
| `release_officer` | 产品批次放行（与排名角色互不重叠） |
| `auditor` / `admin` | 只读审计 / 跨组织管理 |

### 主要端点

- 指标版本：`POST /metrics`（同 `metric_id` 重复提交生成新版本，`code` 不可变）、`GET /metrics?metric_id=`、`POST /metrics/retire`
- 对标周期：`POST /cycles`、`POST /cycles/metrics`（周期锁定指标版本，冻结后不可变）、`POST /cycles/sites`、`POST /cycles/adjustments`（可比性调整系数及依据）、`POST /cycles/freeze`、`GET /cycles/ranking?cycle_id=`、`POST /cycles/publish`、`GET /publications?cycle_id=&version=`
- 证据：`POST /evidence`（校验适用产品、采样窗口、方法版本与周期锁定一致）、`GET /evidence?cycle_id=&site_id=&metric_id=`
- 异常值：`POST /exclusions/request`、`POST /exclusions/review`（独立人员，批准时固化全部工厂排除前后名次）、`GET /exclusions?exclusion_id=`
- 异议：`POST /disputes`、`POST /disputes/resolve`（`upheld` 维持 / `corrected` 登记更正分，原冻结分保留）、`POST /disputes/withdraw`、`GET /disputes?cycle_id=`
- 整改闭环：`POST /findings`、`GET /findings?cycle_id=`、`POST /rectification-plans`（关联发现、责任人、期限）、`POST /verifications`、`POST /verifications/review`（独立人员复验）、`GET /rectification-plans?plan_id=`
- 批次放行：`POST /batch-releases`、`GET /batch-releases?site_id=`（决定不可改写）
- 分数追溯：`GET /trace-score?cycle_id=&site_id=&metric_id=` —— 返回指标版本与规则、可比性调整、逐批证据与哈希、排除记录、冻结计算明细、异议与更正、历次发布状态

### 关键规则

1. **版本化**：指标定义（适用产品、采样窗口、检测方法代码与版本、计算规则、可比性调整）整体版本化；周期在加入指标时锁定版本，后续换版不影响在评周期。
2. **冻结**：周期冻结时逐格落库分数、调整系数、参与/排除的证据清单与结构化计算明细；有未完成审查的排除申请时禁止冻结，冻结后证据与调整不可变。
3. **异常值排除**：只能在冻结前申请；审查人不得来自证据所属工厂组织；批准时对指标名次与总分名次分别留存排除前后快照。
4. **异议与发布**：发布采用不可变版本清单（manifest 含哈希）。未裁定异议的工厂-指标格标记 `held_disputed`，该格及涉事工厂总分暂停，其他指标与其他工厂照常排名；异议裁定后重新发布生成新版本，历史版本永不改写。
5. **整改闭环**：整改计划必须挂在具体发现上，含责任人与期限；复验证据由独立人员接受后才关闭计划；任何整改动作都不会修改已发布排名。
6. **防掩盖**：任一指标缺测的工厂没有加权总分（不会因指标组合而获得虚高排名）；评分按指标方向归一化到 0–100 后加权。
7. **权限分离**：排名管理角色（`quality_manager`/`reviewer`/`operator`）一律不能决定批次放行，只有 `release_officer`/`admin` 可以，放行决定不可改写。
