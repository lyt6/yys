# 网易阴阳师藏宝阁数据采集与预览

本项目通过真实 Chrome、账号独立的持久化 Profile 和 Playwright 打开藏宝阁页面。页面自身发出官方请求，程序只监听并解析装备列表相关响应，不脱离浏览器重放私有接口，也不绕过验证码、短信验证、访问控制或限流。

数据以 SQLite 为唯一事实来源：同一唯一 ID 更新，不存在的 ID 新增，历史项目永不自动删除。本地预览页直接读取 SQLite，不再生成重复的 JSON/CSV 快照。

## 核心架构

```text
账号池中的当前账号
       │
       ▼
独立 Chrome Profile ── 已登录 ─────────────┐
       │                                   │
       └─ 跳转登录页 → 自动填写账号密码 ────┤
                                           ▼
                                  装备接口验证会话
                                           │
                        ┌──────────────────┴─────────────────┐
                        ▼                                    ▼
                 页面滚动并监听响应                     DOM 有限补充
                        │                                    │
                        └─────────── 规范化与去重 ────────────┘
                                           │
                              唯一 ID + 内容哈希 Upsert
                                           │
                                           ▼
                    SQLite 事务（项目、检查点、每轮采集记录）
                                           │
                                           ▼
                                      本地预览页
```

## 这版解决的问题

- 同一 ID 在一轮内多次出现时，按响应顺序保留最后一次观察，不再保留旧价格或旧详情。
- 不同 ID 即使同名同价也分别保存。
- 单轮重复 ID 在变更统计和持久化前统一去重。
- 接口白名单同时校验 HTTPS、官方域名和精确路径，不采集其他域名或相似路径。
- 响应解析任务在生成结果前全部等待完成，降低最后一个响应丢失的概率。
- 支持页面主滚动区或内部滚动容器，并识别明确的接口/页面结束标志。
- SQLite 使用 WAL、完整同步和单事务 Upsert，并在每次操作后显式关闭连接。
- SQLite 是唯一数据源，不再维护 JSON/CSV 导出及旧 JSON 自动迁移链路。
- 扫描周期、最近深扫时间和运行历史跨重启保存；采集开始即落库，异常退出会标记为 `interrupted`。
- 调度以实际开始时间为基准，不再变成“扫描耗时 + 固定等待”的累计漂移。
- 重启会延续上次轮询间隔；手机验证、限流、拒绝访问等风控状态默认冷却 15 分钟，反复重启不会提前请求。
- Chrome/Profile 启动失败会有限重试，不会结束或清理机器上的其他 Chrome 进程。
- 每个账号/目标在初始化数据库前先取得单实例锁，避免两个进程共用 Profile、迁移数据库或互相覆盖结果。
- 所有相对运行路径固定相对于项目目录解析，计划任务从其他工作目录启动也不会换数据库或 Profile。
- `interactive_browser.py` 使用账号池当前账号的同一 Profile，不再验证错误的会话目录。

## 数据合并规则

唯一键按以下优先级获取：

1. `equip_id`
2. `listing_id`
3. `ordersn` / `order_sn`
4. `id`
5. `sn`
6. `role_id` / `roleid`

没有任何稳定 ID 时，使用“名称 + 等级”的哈希降级键，并以 `identity_stable=false` 明确标记。这类记录无法可靠区分同名同等级项目，预览页会显示“降级键”。

内容哈希比较名称、价格、等级和详情，并忽略请求 ID、跟踪 ID、服务端时间、推荐排名、浏览次数等请求级或统计级字段。价格或实际详情变化仍会计为更新。

每条记录保存：

- `first_seen_at`：首次发现时间
- `last_seen_at`：最近一次被页面观察到的时间
- `last_changed_at`：最近一次内容变化时间
- `seen_count`：累计观察次数
- `identity_stable`：唯一键是否来自稳定业务 ID

程序不会删除未再次观察到的项目。它保存的是历史累计集合，而不是严格的当前在售列表；请结合 `last_seen_at` 判断记录的新旧。

## 扫描与分页策略

默认策略：

- 启动时如果没有有效深扫检查点，先执行深度扫描。
- 普通扫描每 60 秒调度一次，最多滚动 20 轮。
- 深度扫描默认每 3600 秒执行一次，最多滚动 100 轮。
- 每轮滚动后按“本轮累计观察到的唯一 ID 数量”判断是否加载了下一批数据。
- 连续 3 轮没有观察到更多唯一 ID 时，以 `termination_reason=idle` 停止，但不会宣称已经扫描到底。
- 只有接口明确返回 `has_more=false`、`is_end=true` 等字段，或页面出现明确的“没有更多”标记时，`scan_complete=true`。

这能覆盖“前几页都是已知项目、较深位置才有批量更新”的情况，因为停止条件不再使用“相对于旧快照的变更数”。

如果官方响应未来出现稳定的更新时间、页码、游标或 `has_more` 字段，解析器会优先使用明确结束字段。当前不能凭空追加页面没有使用的 `since` 参数；没有官方游标和稳定排序时，任何有限深度的分钟级增量扫描都无法数学上保证覆盖全站变化，定时深扫仍然必要。

这里的“全部数据”始终限定于当前目标 URL、页面筛选条件、账号权限和最大滚动轮数所能加载的记录，不代表藏宝阁全站数据。

## 安装

建议使用 Python 3.10–3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

程序默认调用本机 Chrome。机器没有 Chrome 时可使用 Playwright Chromium：

```powershell
$env:CBG_BROWSER_CHANNEL = ""
```

## 配置账号池

复制示例文件：

```powershell
Copy-Item account_pool.example.py account_pool.py
```

然后只编辑本地的 `account_pool.py`：

```python
ACCOUNT_POOL = [
    {
        "name": "temp_account_1",
        "username": "your_account",
        "password": "your_password",
        "profile_dir": "./browser_profiles/temp_account_1",
        "enabled": True,
    },
    {
        "name": "temp_account_2",
        "username": "another_account",
        "password": "another_password",
        "profile_dir": "./browser_profiles/temp_account_2",
        "enabled": True,
    },
]

ACTIVE_ACCOUNT_INDEX = 0
```

规则：

- `ACTIVE_ACCOUNT_INDEX` 选择本进程运行的账号。
- 每个启用账号必须使用不同的 `profile_dir`。
- 可在账号项中设置独立的 `target_url` 或 `database_path`。
- 默认所有账号可安全共享 `data/cbg.sqlite3`，表的主键包含账号和目标条件。
- `profile_dir`、`database_path` 和锁文件的相对路径都基于项目目录，不受当前工作目录影响。
- 验证或限流发生后不会自动切换账号继续请求。
- `account_pool.py` 和 `browser_profiles/` 均被 Git 忽略；浏览器 Profile 内的登录 Cookie 同样属于敏感凭据。

账号密码按你的需求保存在本地 Python 变量池中。不要把 `account_pool.py` 上传、同步或发送给其他人。

## 启动采集

持续运行：

```powershell
python main.py
```

只执行一轮：

```powershell
$env:CBG_RUN_ONCE = "true"
python main.py
```

单次模式同样遵守上次请求时间和安全冷却。如果已人工确认可以立即复查，可仅对这次进程设置 `$env:CBG_IGNORE_RESTART_COOLDOWN = "true"`；不要把它长期配置在自动重启服务中。

程序只在目标页面跳转到登录页时自动填写账号密码。Profile 登录态有效时会直接采集，不重复登录。Cookie 只作为候选登录态，最终必须由目标装备接口和有效响应结构确认。

自动登录对两套网易表单分别处理：邮箱表单直接点击可见的 `j-mail-clause-span` 方框，并只以组件类名 `u-dl-agree-select` 判断已同意；不能使用其隐藏 checkbox，因为该 checkbox 在视觉空框时反而是 `checked=true`。手机表单仍按普通 checkbox 语义处理。若提交后出现“需要同意相关条款”等提示，邮箱方框会按组件状态重新触发并仅重试提交一次。连续失败时会在 `data/login_diagnostics/` 保存只包含协议行、不包含账号密码的提交前与拒绝后截图，方便精确诊断。普通账号密码或协议提交失败只使用正常轮询间隔，不会进入 15 分钟风控冷却。更新这部分代码后需要停止并重新启动现有采集进程，运行中的 Python 不会自动加载新逻辑。

遇到滑块或手机验证时，自动程序会停止。需要时使用当前账号的同一 Profile 人工完成：

```powershell
python interactive_browser.py
```

人工验证工具不能与 `main.py` 同时使用同一账号，账号级锁会阻止这种情况。先停止采集服务；验证成功后会在 SQLite 中记录 `manual_verification`，从而解除之前验证/限流状态的长冷却，后续仍保留正常轮询间隔。

### 服务重启行为

- 程序启动后先取得账号/目标锁，再打开数据库和 Profile。
- 上一轮若已完成，重启只等待该轮 60 秒调度间隔中尚未走完的部分，不会立刻重复访问。
- 上一轮若是手机验证、429、401/403 或明确业务错误，默认从失败完成时间起冷却 900 秒。
- 上一轮若只是 `login_required`，仅等待正常 60 秒调度间隔的剩余部分，修正配置后无需等待 15 分钟。
- 上一轮若在提交结果前被强制终止，已持久化的 `running` 记录会变为 `interrupted`，周期号不会重用，并从恢复时刻保留正常调度间隔。
- SQLite 已提交的数据、检查点和预览页不受采集进程重启影响；未提交事务由 SQLite 自动回滚。
- Chrome 正常退出失败留下临时 Profile 锁时会先有限重试；程序不会杀死不属于本项目的 Chrome。

## 启动预览页

采集程序与预览页可以同时运行：

```powershell
python preview_server.py
```

打开：<http://127.0.0.1:8765>

预览页支持：

- 按账号和目标范围筛选
- 按名称、业务 ID 或唯一键搜索
- 按最近变化、最近观察、首次发现或观察次数排序
- 查看稳定 ID、降级键和 24 小时变化统计
- 查看项目原始业务详情和最近采集历史

预览服务默认只监听 `127.0.0.1`，没有登录认证，不要直接暴露到公网。指定其他数据库或端口：

```powershell
python preview_server.py --database data/cbg.sqlite3 --port 9000
```

## SQLite 数据结构

默认数据库：`data/cbg.sqlite3`

- `listings`：项目累计快照，以账号、目标条件和唯一键组成主键。
- `scan_runs`：每轮周期号、`running/finished/interrupted` 状态、开始/结束时间、模式、观察数、新增数、更新数和失败原因。
- `checkpoints`：当前周期、最近成功时间、最近深扫时间和深扫结束状态。
- `schema_info`：数据库结构版本。

数据库结构会从旧版 v1 原地升级到 v2，并保留已有项目、采集记录和检查点。升级只增加采集周期与运行状态字段。

WAL 模式运行期间，不要只复制 `cbg.sqlite3` 作为在线备份，因为最新提交可能仍在 `cbg.sqlite3-wal`。可靠做法是先停止采集和预览后复制数据库，或使用 SQLite backup API。

## 运行配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CBG_TARGET_URL` | 规范装备列表页 | 当前筛选条件；账号项 `target_url` 优先 |
| `CBG_DATABASE_PATH` | `data/cbg.sqlite3` | SQLite 路径；账号项 `database_path` 优先 |
| `CBG_BROWSER_CHANNEL` | `chrome` | 留空使用 Playwright Chromium |
| `CBG_HEADLESS` | `false` | 无头模式可能提高重新验证概率 |
| `CBG_POLL_INTERVAL_SECONDS` | `60` | 调度间隔，最低 30 秒 |
| `CBG_INCREMENTAL_PAGES` | `20` | 普通扫描最大滚动轮数 |
| `CBG_FULL_REFRESH_INTERVAL_SECONDS` | `3600` | 深度扫描间隔，最低 300 秒 |
| `CBG_MAX_PAGES` | `100` | 深度扫描最大滚动轮数 |
| `CBG_SCROLL_DELAY_MS` | `3000` | 滚动后等待响应时间，最低 1000ms |
| `CBG_IDLE_ROUNDS` | `3` | 连续无新增观察的停止阈值 |
| `CBG_RUN_ONCE` | `false` | `true` 时仅执行一轮 |
| `CBG_RESTART_COOLDOWN_SECONDS` | `900` | 手机验证、限流、拒绝访问等风控状态的重启冷却，最低 60 秒 |
| `CBG_IGNORE_RESTART_COOLDOWN` | `false` | 手工确认后仅跳过本次启动等待；不建议用于常驻服务 |
| `CBG_BROWSER_START_ATTEMPTS` | `3` | Chrome/Profile 启动最大尝试次数 |
| `CBG_BROWSER_RETRY_DELAY_SECONDS` | `3` | 浏览器启动重试基础等待秒数，后续按次数递增 |

一分钟频率不是“模拟真人”的保证，也不保证不会触发站点风控。若出现 429、403、手机验证或频繁重新登录，应降低频率并按服务端要求处理。

Windows 计划任务或服务重启后，不会继承你在另一个 PowerShell 窗口临时设置的 `$env:...`。稳定配置应写入计划任务/服务的启动环境；账号密码仍只放在被 Git 忽略的 `account_pool.py`。默认有界面模式需要交互式桌面，计划任务应选择“仅当用户登录时运行”；会话 0 服务通常只能使用无头模式，而无头模式可能更容易触发重新验证。

## 状态和停止规则

- `authenticated`：装备接口或明确空列表已验证成功。
- `login_required`：账号密码未建立可用登录态，或登录后仍返回登录页。
- `mobile_verification_required`：页面或接口要求手机验证。
- `rate_limited`：装备接口返回 HTTP 429。
- `access_denied`：装备接口返回 HTTP 401/403。
- `business_error`：最近的装备接口响应明确返回失败业务状态。
- `data_unavailable`：页面打开但没有有效数据结构；连续三次后停止。
- `navigation_error`：页面加载失败；连续三次后停止。
- `collector_error`：未预期的采集器异常；连续三次后停止并保留错误记录。
- `browser_unavailable`：浏览器或 Profile 无法启动。
- `interrupted`：进程在一轮完成前退出，启动/退出恢复流程已将该轮安全收尾。
- `manual_verified`：人工工具已确认同一 Profile 的目标装备接口可用。

认证、访问控制、限流和业务错误立即停止；不会自动轮换账号绕过限制。解析失败会记录不含字段值的接口结构摘要，便于安全适配页面变化。

## 测试

```powershell
python -m pip install -r requirements-dev.txt
ruff check .
python -m pytest -q
```

测试全部离线运行，不访问藏宝阁，也不读取本地 `account_pool.py`。GitHub Actions 会在 Python 3.10 和 3.12 上执行同样的检查。

## 项目结构

```text
account_pool.example.py  # 可提交的账号池模板
account_pool.py          # 本地明文账号池，Git 忽略
main.py                  # 采集入口、账号配置、重启保护与 SQLite 编排
cbg_fetcher.py           # 浏览器、登录、响应监听和扫描核心
data_model.py            # 唯一键、内容哈希、去重与 Upsert 规则
storage.py               # SQLite、检查点和账号级单实例锁
interactive_browser.py   # 必要时的同 Profile 人工验证工具
preview_server.py        # 只读本地预览服务
web/                     # 预览页静态资源
tests/                   # 离线单元与集成测试
```

## 已知边界

- 自动化浏览器仍然是自动化工具，不等同于真人操作，无法保证永不触发验证。
- 登录页选择器、滚动容器、响应路径或字段结构变化时仍需适配。
- 没有官方增量游标或稳定排序时，有限深度扫描无法证明覆盖所有历史位置。
- 不删除策略会持续增加 SQLite 体积，并保留已经下架或长期未观察到的历史项目。
- 降级唯一键无法绝对区分同名同等级且无稳定 ID 的不同项目。
