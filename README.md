# 跨境电商供应链管理Agent助手

当前版本 **v0.10.1 · M6.1「后台分析恢复」** 已完成本地模拟验收。在 M6 流程复盘与受控 Agent 的基础上，新增启动后持续恢复、有限自动重试、人工重试、持久任务列表和处理记录。迁移 10 增加任务历史与重试字段，继续使用统一库存及人工确认边界。

接手开发请先阅读 [最新 M6.1 交接补充](docs/项目开发交接补充_M6.1_2026-10-26.md)。[M6 交接](docs/项目开发交接补充_M6_2026-10-25.md)、[M5 交接](docs/项目开发交接补充_M5_2026-10-25.md)、[M4 交接](docs/项目开发交接补充_2026-10-25.md) 与 [原项目交接](docs/项目开发交接文档_2026-10-24.md) 保留历史状态，当前入口与后续工程以最新补充为准。

本轮仅使用模拟数据，无需模型密钥。使用方法见 [M6.1 交付说明](docs/M6.1_后台分析恢复交付说明.md)，验收结果见 [M6.1 验收摘要](docs/M6.1_验收摘要.json)。[M6 复盘与 Agent](docs/M6_复盘与Agent交付说明.md)、[M5 采购与计划](docs/M5_采购与计划交付说明.md)、[M4 履约与协同](docs/M4_履约与协同交付说明.md)、[M3 盘点质量与异常](docs/M3_盘点质量与异常交付说明.md)、[M2 模拟与隔离](docs/M2_模拟与隔离交付说明.md) 及原工作流继续保留；需求基线见 [PRD v2.0](docs/跨境电商供应链管理Agent助手_大版本更新_PRD_v2.0.md)。本轮仅验证确定性工具和模型模拟响应，真实外部服务及生产部署尚未验收。

v0.4.1 的四个阶段说明保留在 docs 中作为历史记录；当前版本仍保留既有采购、调拨、退货、质检、审批及受控 Agent 建单能力。

## 启动

需要Python 3.12+及uv；或使用Python venv/pip等价安装。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
./scripts/run.sh
```

本机依赖已安装，可直接运行最后一条命令。服务只监听本机 `127.0.0.1:8010`。

本轮已启动的 M6.1 独立开发工作台为 <http://127.0.0.1:8026/>；账号文件、重启方式及旧服务版本见 [最新交接补充](docs/项目开发交接补充_M6.1_2026-10-26.md)。下列 8010 为默认启动地址，旧进程需要先备份并重启才会加载新版本。

- 工作台：http://127.0.0.1:8010
- API文档：http://127.0.0.1:8010/docs
- 登录信息：`.local/login.txt`，首次初始化生成随机密码，权限0600；不要提交版本库。
- 岗位账号：admin / warehouse / cashier，密码各不相同。
- 数据库：`.local/inventory.db`（SQLite WAL模式）。重启不会重新导入演示数据。
- `APP_DATA_DIR` 可指定绝对数据目录；默认路径锚定项目目录，不随启动位置漂移。API与数据库运行指纹不符时拒绝启动。升级前须备份；恢复至新路径使用验证恢复命令，不直接复制已绑定的数据库。

不要将此版本开放到公网或录入真实客户隐私。生产认证、多用户及混合负载容量、自动异地备份和真实连接器仍需后续实施。本地启动自动执行已登记的数据库升级；升级前请备份数据库，切勿直接改已应用的迁移。创建新的生产数据库时不要调用演示bootstrap；当前运行脚本是本地演示入口。

## 模拟管理与开发数据

当前受管理模拟进程支持本机 macOS，使用系统沙箱阻止跨空间文件访问；其他系统拒绝启动，不会降级为无隔离运行。PostgreSQL 验收使用受限账号，要求密码认证。

```sh
./scripts/simulator.sh init
./scripts/simulator.sh create --config simulator/scenarios/development.json --run-id dev-workbench
./scripts/simulator.sh run dev-workbench
./scripts/simulator.sh web --port 8020
```

模拟管理页面为 http://127.0.0.1:8020 ，独立管理凭据在 `.local/simulator/admin-token.json`。另开终端执行 `./scripts/simulator.sh workbench dev-workbench --port 8021` 可使用生成数据打开业务工作台；业务账号与密码位置见 M2 说明。重置创建后继空间并保留旧证据，运行中的空间无法清理。

## 测试

```sh
.venv/bin/python -m pytest -q
```

当前验收结果见 [M6.1 验收摘要](docs/M6.1_验收摘要.json)：SQLite 387 通过、1 跳过，PostgreSQL 386 通过、2 跳过；本版 240 组独立场景、440 次运行、5,420 条业务检查通过，九类固定场景通过。新增队列回归、真实进程退出接续和浏览器账号切换隔离均通过。M6 基准/改善配对只计一组，开发集和浏览器操作不计入独立数量。v0.10.0 的 1,000 组验证和三档 SQLite 容量数据保留在 [历史 M6 摘要](docs/M6_验收摘要.json)，本版未重跑容量。旧无凭证收货/交货仅在显式迁移兼容档位测试，默认入口另有拒绝绕过测试。模型仅用模拟响应，没有真实 API 调用。

额外生成100组 M6 配对独立场景（200次运行）：

```sh
.venv/bin/python scripts/verify_m6.py --count 200 --seed-start 2800000000 --root /absolute/new-m6-validation-root
```

整版 SQLite 验证可运行 `scripts/verify_m6_release.py --root /absolute/new-release-root`，生成500组独立场景；双数据库流程见 M6 交付说明。

额外生成100份 M5 独立验收数据：

```sh
.venv/bin/python scripts/verify_m5.py --count 100 --root /absolute/new-m5-validation-root
```

额外生成100份 M4 独立验收数据：

```sh
.venv/bin/python scripts/verify_m4.py --count 100 --root /absolute/new-m4-validation-root
```

额外生成100份 M3 独立验收数据：

```sh
.venv/bin/python scripts/verify_m3.py --count 100 --root /absolute/new-m3-validation-root
```

额外生成100份 M2 独立验收数据：

```sh
.venv/bin/python scripts/verify_m2.py --mode batch --count 100 --root /absolute/new-validation-root
```

每次指定一个新目录，避免覆盖或混用开发数据。

## 技术结构

```text
app/
  config.py      绝对路径、运行指纹与环境配置
  clock.py       业务时钟；认证和超时继续用真实时间
  review.py      后台分析、不可变快照、时间订正、改善与权限复查
  review_sources.py 授权事件及历史流程时间重建
  review_metrics.py 分位数、在制、显式依赖与限制候选
  review_agent.py 受控解释、跟踪及待确认方案工具
  review_worker.py 持续恢复线程、生命周期与独立 CLI
  review_queue_schema.py 迁移 10：重试计划与不可变处理历史
  m6_api.py      M6 严格契约及分页入口
  supply.py      采购承诺、关联补发、分批供给、到货检验及独立差异
  planning.py    需求日证据、供给去重、规则补货及人工确认
  m5_api.py      M5 严格契约、岗位与仓库授权入口
  fulfillment.py 销售订单、分配、拣货复核、交接、取消及供给关联
  connectors.py  模拟账号、Inbox、源事件与业务动作去重
  delivery.py    可靠外发、租约、原业务编号查询与模拟适配器
  delivery_worker.py 独立模拟消息处理进程
  reconciliation.py 快照水位、连续增量、差异证据及异常任务
  m4_api.py      M4 严格契约、岗位与连接权限入口
  counting.py    盘点范围锁、快照、初盘复盘、差异审批
  quality_control.py 检验批次、受保护数量及双人处置
  exception_tasks.py 唯一异常实例、分派、复核和历史
  m3_api.py      M3 严格请求契约与权限入口
  catalog.py     供应商、经营关系、库存知识与来源映射
  inventory.py   统一库存记账、收发凭证及请求去重
  commitments.py 渠道额度与订单行占用状态转换
  events.py      不可变业务事件、时间来源和更正
  v2.py          M1 新接口
  main.py        FastAPI路由、校验、会话、权限及静态资源
  migrations.py  有序版本迁移（已有数据库升级）
  workflows.py   导入、采购、审批与冲正领域事务
  phase2.py      第二阶段请求模型及接口
  operations.py  调拨、退货及质检事务
  accounts.py    账号权限与管理审计
  backup.py      SQLite在线备份及独立恢复
  phase3.py      第三阶段请求模型及接口
  agent_model.py DeepSeek接口及工具参数校验
  agent_queries.py 权限范围内的库存与异常只读查询
  agent_actions.py 待确认方案、原子建单及执行审计
  phase4.py      助手接口、会话复查、限流与并发控制
  db.py          SQLAlchemy模型及事务管理
  domain.py      收货/交货领域事务、库存和额度约束
  security.py    密码哈希与会话token摘要
  bootstrap.py   显式初始化本地演示资料
  static/        响应式工作台，无CDN和前端构建依赖
simulator/       场景生成、控制面、受限业务进程、调度重放、独立判定及管理页面
tests/           回归、并发、跨运行隔离与故障恢复测试
docs/            阶段范围、接入契约、OpenAPI和PostgreSQL DDL
```

默认库存归属当前登录商户；多货主操作未实现。外部仓只读。M4 新增的 connector_outbox 通过本地模拟适配器发送；原库存 Outbox 继续保留变动记录，不自动补发历史数据。应用未暴露直接修改余额、删除流水接口，真实外部回写保持关闭。

数据库URL可通过 `DATABASE_URL` 配置成 `postgresql+psycopg://...`，驱动已安装，当前迁移 10 DDL 见 `docs/schema-postgresql.sql`。本轮已通过 SQLite / PostgreSQL 回归与模拟验证；M6 SQLite 数据库副本升级后 87 张原业务表原列内容一致，并完成备份恢复校验。早期 PostgreSQL 恢复演练记录保留在历史文档中；生产基础设施及多用户/真实业务容量尚待验证。

收货/交货超时后前端保留原请求并使用相同幂等键重试。请求键处理传输重试；跨浏览器或新表单必须沿用相同业务凭证，系统再按业务层去重。采购收货另以采购单+到货凭证号防止重复业务，并限制累计实收不超过采购量；当前工作台的普通收货及线下交货也按来源主体和到货/交货子凭证做业务去重；旧无凭证入口默认禁止新增，仅保留原请求重放。迁移兼容开关与限制见 M1 说明。

## 第二阶段操作入口

- 批量导入：管理员先下载模板，填写UTF-8 CSV并预览，通过后确认整批导入。期初仅适用于无库存历史的仓库商品组合。
- 采购与收货：仓管或管理员新建多商品采购单，再用唯一到货凭证分批登记良品/待验/残次。
- 调整与冲正：使用warehouse申请，再切换admin审核；申请人不能审批自己的申请。
- 迁移与审批历史随数据库保存；本地账号密码、数据库和备份均不提交Git。

## 第三阶段操作入口

- 仓间调拨：预占→发出→分批签收；在途短少由另一管理员审核。
- 退货与质检：客户实物退回先待验，质检后分流；供应商退货关联原收货出库。
- 账号与试运行：岗位/仓库授权、停用撤销会话、库存约束检查、SQLite备份及独立恢复。
- 外部平台连接器尚未接入；第四阶段新增Agent查询和确认建单，真实模型待配置密钥联调。

## PostgreSQL验证

准备独立测试实例，将SQLAlchemy连接URL保存在本机0600权限文件中，勿提交Git。测试账号需创建schema权限，恢复演练额外需要创建数据库权限。仅在测试实例运行：

```sh
TEST_DATABASE_URL_FILE=.local/pg-trial-url .venv/bin/python -m pytest -q --tb=no
.venv/bin/python scripts/postgres_verify.py --url-file .local/pg-trial-url --bin-dir /path/to/postgresql/bin
```

提供与服务端兼容的pg_dump/pg_restore即可；可选requirements-pg-test.txt包含本地隔离测试运行时。恢复报告、归档与凭据均留在.local中。

## 第四阶段Agent助手

打开工作台侧栏「Agent助手」，选择仓库和商品。无需密钥可点击「查询库存」或「检查异常」，结果来自本机账本并显示数据依据。

模型按DeepSeek V4.1 Flash（deepseek-flash）接入。后续设置服务端DEEPSEEK_API_KEY，或将密钥保存到.local/deepseek-api-key并设置0600权限，刷新页面后可使用自然语言提问。程序不自动读取.env。请勿将密钥写入聊天、业务备注或版本库。

只发送当前问题和显式选中的商品/仓库名称给DeepSeek；结果由本地查询及固定规则生成。每次提问独立解析，最近10条记录仅留在页面内存。查询分析不会建单或修改库存。

管理员和仓管可进入「辅助建单」，通过表单生成单商品调拨、采购或库存调整的待确认方案；无密钥也可使用。核对并勾选确认后，才会创建调拨草稿、待收货采购单或待审批调整申请。此步骤不改库存、不占用调拨库存、不付款或发送供应商通知。库存调整仍由另一管理员审批。

待确认方案30分钟有效，确认时复查身份、仓库权限、库存和授权版本；重复确认只返回同一业务单据。不同方案之间不保证业务去重。待确认方案及执行审计持久化保存，可在「执行审计」查看；自然语言生成方案已完成模拟契约测试，待配置密钥后做真实服务联调。

## M6 复盘入口

「流程复盘」支持按期间和授权范围生成报告，查看时间覆盖、分位数、未完成量、候选压力证据与订单依赖。时间订正追加新证据，旧报告不变；改善任务不改库存。原始承诺、最新承诺及登记延迟分别展示，缺少实际时间或明确关联时保持未知。

「分析任务」持久保存状态和处理记录。服务启动后自动接续排队及租约到期的任务；临时故障有限重试，人工重试需填写原因并复查原创建人权限。管理员可查看当前服务进程的恢复状态；具体重试规则和独立进程配置见 [M6.1 交付说明](docs/M6.1_后台分析恢复交付说明.md)。

复盘与计划助手提供规则解释、采购跟踪和补货方案计算；无需密钥。补货方案仍由创建者在「规则补货」页核对确认，随后按实际业务执行。完整口径、恢复方式、权限和已知限制见 [M6 交付说明](docs/M6_复盘与Agent交付说明.md)。
