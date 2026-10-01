# 种质资源入库与活力复检服务

本项目是面向种质资源库的 Python 后端服务，用于登记采集或引进材料、建立种子批次、管理低温库位和容器移动、执行发芽活力检测、生成复检日程并处理环境与质量告警。档案、库存、检测和发放审批都保存在本地 SQLite 中，关键写入带版本或幂等键，适合在单个 Linux 应用容器内运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `data/germplasm.db`，也可以通过 `GERMPLASM_DATABASE_PATH` 指向其他 `.db`、`.sqlite` 或 `.sqlite3` 文件。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查为 `GET /api/system/health`。首次使用可调用 `POST /api/auth/bootstrap` 创建管理员，再通过 `POST /api/auth/login` 取得 Bearer 会话令牌。种质业务接口统一位于 `/api/germplasm`。

## 测试与构建检查

```bash
python -m pytest
python -m compileall -q app tests
```

下面两条命令分别检查 HTTP 入口和完整的入库演示链路：

```bash
python -m app.cli smoke
python -m app.cli demo
```

## 业务边界

- `app/germplasm/accessions.py` 管理来源、资源档案、护照信息与接收状态。
- `app/germplasm/inventory.py` 管理批次、库位容量、容器摆放、移动、领用和冻结。
- `app/germplasm/viability.py` 管理检测规程、取样、重复计数、活力结果与复检日程。
- `app/germplasm/quality.py` 管理温湿度读数、偏离告警和种质发放审批。
- `app/germplasm/duplicates.py` 管理疑似重复识别、人工判定、合并预演与安全执行。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 疑似重复与人工合并

年度清理中同一份地方品种可能由旧系统和合作站分别导入，资源号不同但学名、采集地点和护照字段高度接近。系统采用"只建议、不改档"的两阶段流程：

1. `POST /api/germplasm/duplicates/scan` 运行评分规则（`duplicate-rules-v1`），仅生成或刷新候选；每条候选带逐字段证据（学名、来源地、采集地、采集者、经纬度、别名等）、评分、规则版本、档案行版本与快照。扫描幂等，已判"无关/已合并"的候选不会复活。
2. 审阅人通过 `POST /api/germplasm/duplicates/candidates/{id}/decision` 判定**无关（unrelated）、暂缓（defer）或重开（reopen）**，判定与候选行版本绑定。
3. `POST /api/germplasm/duplicates/candidates/{id}/merge-preview` 选择保留档案并对每个冲突字段逐项给出保留决定（含原值与最终值），预演来源/护照别名/限制归并、链式收敛范围与合并前引用图。预演幂等，相同决定重复提交不产生新记录。
4. `POST /api/germplasm/merges/{id}/execute` 携带候选版本与幂等键执行：批次改挂保留档案（检测、计数、复检日程、移动记录随批次自动归属且不变），事件历史复制归并，来源出处与限制按最严格并集落到保留档案，旧档案冻结为 `merged` 而不删除，旧资源号登记别名并始终直指向最终保留档案。
5. 已签发的发放记录与检测结果不可变：发放明细仍指向冻结档案，仅记录在审计理由和合并后引用图中。
6. 合并防止循环或链式重复：冻结档案不可复活，已作过保留根的档案不能再被并入他处，扇形归并（多份新档案并入同一保留根）允许；历史别名闭包在执行时扁平化。
7. `GET /api/germplasm/merges/{id}` 与 `/graph/{before|after}` 可重放合并前后的完整引用图、逐项字段决定原值与审计理由；`GET /api/germplasm/accessions/by-no/{accession_no}` 用旧资源号解析到保留档案。

合并操作需要 `accessions.merge` 权限（默认授予资源审核员 curator）。


## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。资源档案、库位、容器摆放、检测任务和发放申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。活力检测保留采用的规程版本和每个重复的观察计数，完成后可依据作物及风险策略生成下一次复检日期。会话令牌只保存摘要，审计记录不保存明文密码或令牌。
