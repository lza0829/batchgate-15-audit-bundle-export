# 02-batchgate（闸序）

Batchgate — 离线作业依赖编排：作业定义 → 依赖就绪检查 → 执行/失败重试 → 产出水位与对账。

与 Feedstitch（多源 Feed 归并）主链路不同，禁止做成 Feed 解析/冲突/回看换皮。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5200 |
| API | http://localhost:10200 |

## 主链

配置作业与依赖 → 触发一次编排 Run → 按依赖顺序执行夹具作业 → 写入尝试记录与产出水位 → 报告对账

## 种子说明

- **ingest_orders**：无依赖，写出订单水位。
- **dim_customer**：无依赖，写出客户维表水位。
- **fact_order_daily**：依赖上两者成功且水位推进后才可执行（底座仅做“上游成功”检查，严格水位闸与重试预算留给 0-1 题）。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 审计包导出

编排完成后可在编排台对指定编排导出审计包（zip，落盘于 `DATA_DIR/audit_exports/`，容器内即 `/data` 卷）：

- 包内容与库内记录同源：`report_summary.json`（报告摘要）、`attempts.csv` + `attempts/attempt_*.json`（每条尝试的状态、原因与证据，含 skipped_dep / failed）、`dependencies/`（作业依赖快照）、`manifest.json`（文件名、字节数、SHA-256）。
- 每次导出生成一条 `audit_exports` 记录（同编排版本号递增），同一版本可重复下载、字节与哈希不变；整包 SHA-256 随记录落库。
- 构建时与独立脚本均可核对：`python -I backend/scripts/verify_audit_bundle.py <bundle.zip>` 校验清单哈希、尝试行与证据、报告成功/失败/跳过口径一致。

## 0-1 切口（未实现）

`dep_gate` / `retry_budget` / `watermark_reconcile` / `sla_breach`
