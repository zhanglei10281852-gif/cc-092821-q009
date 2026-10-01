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
- `app/germplasm/duplicates.py` 管理疑似重复识别、人工复核与安全合并。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 疑似重复与合并流程

- `POST /api/germplasm/duplicates/scan` 运行规则集（当前版本 `dup-rules-v1`），按学名、作物、品种、来源地点、护照字段与接收日期打分，达到阈值生成带字段证据的候选；规则只写候选，不改动档案，重复扫描按候选键幂等更新。
- 审阅人通过 `POST /api/germplasm/duplicates/{id}/review` 判定无关（必须填写说明）或暂缓；通过 `POST /api/germplasm/duplicates/{id}/preview` 预演合并，确认保留档案、逐项字段决定与归并影响，预演只读且校验候选版本。
- `POST /api/germplasm/duplicates/{id}/merge` 执行合并：来源与护照差异归入保留档案，冲突字段逐项记录决定与双方原值，批次改挂保留档案，限制按更严格一方合并，事件历史复制归并；旧档案置为退出保存并指向保留档案，旧资源号经 `GET /api/germplasm/accessions/resolve/{accession_no}` 继续解析。
- 合并以幂等键去重，重放返回原合并记录；已并入他档的记录不能再次参与合并，链式指向会被展平，杜绝循环与长链。
- `GET /api/germplasm/merges/{id}` 返回合并前后的完整引用图、逐项字段决定、别名与操作理由，供审计重放。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。资源档案、库位、容器摆放、检测任务和发放申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。活力检测保留采用的规程版本和每个重复的观察计数，完成后可依据作物及风险策略生成下一次复检日期。疑似重复候选同样以版本号做乐观锁，合并执行要求候选版本一致并以幂等键保证重放安全；合并不改动已签发的发放明细与检测结果，旧档案原始字段保留不变。会话令牌只保存摘要，审计记录不保存明文密码或令牌。
