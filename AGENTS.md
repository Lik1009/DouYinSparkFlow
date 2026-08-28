# DouYinSparkFlow 项目交接文档

> 本文件供任何接手此项目的 agent/开发者完整理解项目。请先通读，再动手。
> 最后更新时间：2026-08-27（main 分支）

## 1. 项目是什么

**抖音火花自动续火脚本**：每天自动通过抖音网页版聊天页（`https://www.douyin.com/chat`）给指定好友各发送一条“今日火花”消息，维持好友火花。由 GitHub Actions 定时运行，无人值守。

- 账号：`Sakuro_Mai`（抖音昵称 Sakuro.）
- 目标好友：10 人（见 §4 TASKS）
- 运行环境：GitHub Actions（ubuntu-latest），云端无头 Chromium

## 2. 当前状态（截至 2026-08-14）

- **稳定运行中**：08-09 ~ 08-14 每天 9/9 全发、每人恰好 1 条、无漏发无重复（核对方法见 §8）。
- 发送时间：cron `40 16 * * *`（UTC）= 北京时间次日 00:40；GitHub 延迟后实际约 **北京 01:10~01:40** 送达。（2026-08-27 由 03:00 调整）
- cookies 由 keepalive 每 6 小时自动续期回写（密钥最后更新时间即最近一次续期）。

## 3. 架构与文件

### 工作流（.github/workflows/）

| 文件 | 作用 | cron（UTC） | 备注 |
|---|---|---|---|
| `schedule.yml` | **主发送** | `40 16 * * *` | ⚠️ **修改此文件会触发 GitHub“恶意工作流”审批门禁**，需要仓库所有者手动 Approve；非必要不要改 |
| `catchup.yml` | 兜底触发 | `17 2 / 17 4 / 17 7 * * *` | 查当天是否已有成功发送（用**专用端点** `/actions/workflows/schedule.yml/runs`，created_at 换算北京日期比较；通用端点 `workflow_id` 过滤失效，见 §6.10），没有才 dispatch 主发送；**新会话硬化期（<6h，见 §6.12）内拒绝 dispatch**；新文件可安全修改 |
| `keepalive.yml` | cookies 续期 | `23 */12` + `53 */12` | 用现有 cookies 打开 chat 页触发心跳，抓取最新 cookies，用 `SPARKFLOW_PAT` 回写 `COOKIES_SAKURO_MAI` |
| `review.yml` | 发送后核对 | workflow_run 触发 | 下载 run-logs 工件解析 app.log；cookies 过期发专属邮件（SMTP 未配置则跳过）；识别跳过标记 |
| `schedule_dev.yml` / `schedule_api.yml` | 上游 fork 测试用 | - | 在本仓库为 disabled_fork，忽略 |

### 核心代码

- `core/tasks.py`：全部核心逻辑（见 §5）
- `core/msg_builder.py`：消息模板 + 一言 API 替换 `[API]`
- `core/browser.py`：浏览器启动（本地/CI 区分）
- `utils/config.py`：环境变量 → 配置；`get_userData()` 解析 TASKS + cookies
- `utils/export_github_env.py`：把 GitHub vars/secrets 导出到环境变量和 `.env`
- `utils/logger.py`：日志（注意：handler 级别必须与 logger 同步，已修复）
- `utils/hitokoto.py`：一言 API
- `utils/notify.py`：SMTP 邮件（`send_email`，未配置时跳过不阻塞）
- `utils/review_run.py`：review 工作流的判定逻辑
- `utils/cookie_keepalive.py`：keepalive 工作流的续期+回写逻辑
- `main.py`：入口，加载 .env 后调 `runTasks()`

## 4. 配置

### Secrets

- **环境 `user-data`**：`COOKIES_SAKURO_MAI` — 抖音登录 cookies（JSON 数组；keepalive 会自动规范化并回写）。**这是账号凭证，任何操作不得打印其值。**
- **仓库级**：`SPARKFLOW_PAT` — 经典 PAT（`repo` 权限），用于 keepalive 回写密钥、catchup 查询运行记录。
- **可选（邮件通知）**：`SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `NOTIFY_EMAIL`。未配置时 cookies 过期只靠 GitHub 失败通知。

### Variables（环境 `user-data`）

- `TASKS`（当前值）：
  ```json
  [{"username":"Sakuro.","unique_id":"Sakuro_Mai","targets":["84611333990","57569913835","HOLLOW_LOVE","zjj00000010","25191158994","xiaolangaini","1191371127","heihahou7316","98241180006","72534209781"]}]
  ```
  （2026-08-16 已清理原第二个空任务，不再打 WARNING；2026-08-19 新增好友 `72534209781`，目标 9 → 10 人。）
- `MATCH_MODE=short_id`：匹配用抖音号/short_id（脚本实际同时支持多种标识符，见 §5）
- `MESSAGE_TEMPLATE`：`[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]`
- `HITOKOTO_TYPES=["文学","诗词","哲学"]`，`LOG_LEVEL=Debug`
- `BROWSER_TIMEOUT=120000`、`FRIEND_LIST_WAIT_TIME=2000`、`TASK_RETRY_TIMES=3`

### 仓库 Variables（临时调试用）

- `DEBUG_TARGETS`：JSON 数组，如 `["zjj00000010","84611333990"]`；设置后只发给这些目标。
- `DEBUG_BYPASS_DEDUP=1`：调试时绕过当天去重，允许多轮测试。
- **用完必须清空**（`gh variable delete`），否则生产运行也会被限制。
- `LAST_SEND_DATE`：历史遗留变量（旧去重方案），当前代码不使用；**已于 2026-08-16 删除**。

## 5. 核心逻辑（core/tasks.py）

发送流程（`do_user_task` → `runTasks`）：

1. **登录态检查**：页面正文含 `扫码登录/验证码登录/密码登录` 即未登录 → 快速报错 + 截图（不傻等）。
2. **搜索直达**（首选，稳定）：对每个目标，用 `resolve_nickname()` 从 `userIDDict` 取昵称 → 搜索框输入昵称 → 在 `.SearchPanelitembox` 里精确匹配标题 → 点 `.SearchPanelitemchat_btn`（发消息）打开会话。
3. **当天去重**：打开会话后检查聊天面板（`already_sent_today_in_chat`）——是否存在“我发的 + 含 `今日火花` + 时间为今天”的消息；是则跳过该好友。
4. **发送**：在 `.messageEditorimChatEditorContainer` 输入模板（按 `\n` 分行，Shift+Enter 换行，最后 Enter 发送），发送后截图。
5. **滚动兜底**：搜索失败/无昵称映射的目标，用 `scroll_and_select_user` 滚动扫描 3 轮（每轮回到顶部）。
6. **汇总**：统计 sent/skipped；0 发送但有跳过 → 视为“今天已发送”不算失败（`SKIP_ALL_ALREADY_SENT`）；0 发送且 0 跳过 → 报错。

### 关键机制与坑（务必理解）

- **时区**：浏览器上下文必须 `browser.new_context(timezone_id="Asia/Shanghai")`。抖音按浏览器本地时区显示消息时间；runner 是 UTC，不设时区会把昨天消息显示成 `02:38` 导致跨天误判（曾造成漏发/重复）。
- **时间解析**：`_time_is_today()` 把“刚刚/N分钟前/N小时前/HH:MM/昨天 HH:MM”解析为真实时间（用北京时间 `_beijing_now()`）判断是否今天。
- **防重复**：单次运行 `sent_targets` 去重（每个目标只发一次）+ 发送数超过目标总数即**强制终止**（曾发生 224 条重复事故，这是防线）。
- **跨运行幂等**：当天已发过 → 跳过并记账；重复触发/catchup/手动补发都不会重复发。
- **昵称映射**：`handle_response` 监听 `aweme/v1/web/im/user/info` 响应，按 `short_id/unique_id/sec_uid/uid/昵称/备注` 全部建索引（`userIDDict`）。
- **搜索只匹配昵称**：抖音聊天搜索不支持按抖音号/short_id 搜（实测“未搜索到相关内容”）。
- **失败现场**：`_snapshot_on_failure` 每次失败保存截图 + 页面正文 + URL 到 `logs/screenshots/`，随工件上传。

## 6. 历史踩坑记录（避免重蹈覆辙）

1. **cookies 过期/风控**：失效时页面显示登录落地页，好友列表加载不出。对策：keepalive 每 6h 续期回写；失效时明确报“未登录”，需重新导出 cookies 或本地扫码（`node ~/.config/douyin_keepalive/keepalive.js`）。
2. **创作者中心改版（2026-08-02）**：`creator.douyin.com` 私信页新版本好友列表接口 `imapi.douyin.com` 跨域被 CORS 阻断、DOM 全变，旧代码全部失效 → 迁移到 `www.douyin.com/chat`（参考上游 dev 分支方案）。
3. **滚动列表漏目标**：虚拟化列表 + 发送后排序变动导致漏扫（乐邦/嘿哈吼反复漏）→ 改用**搜索直达** + 3 轮滚动兜底。
4. **runner 时区 UTC**：见 §5，必须 `timezone_id="Asia/Shanghai"`。
5. **“N小时前”跨天**：凌晨运行时昨天下午的消息显示“17小时前”，按格式匹配会误判 → 真实时间解析。
6. **重复发送事故**：匹配后未把目标标记已发送 → 无限循环（zjj 收到 224 条）→ `sent_targets` + 硬上限。
7. **GitHub cron 延迟/丢失**：cron 可能延迟 3-5 小时甚至跳过（官方特性）。对策：主 cron（北京 00:40，2026-08-27 由 03:00 调整）+ catchup 多时段兜底 + 应用层幂等。**不要承诺“准时 9 点”**。
8. **工作流审批门禁**：改 `schedule.yml` 会触发 GitHub“potentially malicious workflow”审批；`gh run rerun`、`gh variable set` 等自管理步骤同样触发。**新工作流文件（catchup/review/keepalive）不受影响，可安全修改。**
9. **review 误报**：0 发送但已有跳过（当天已发）曾误判失败 → 已改为“跳过>0 不算失败”。
10. **catchup 兜底曾完全失效（2026-08-16 修复）**：原实现用通用端点 `/actions/runs?workflow_id=...` 查询主发送，但实测该端点的 `workflow_id` 过滤参数**完全失效**（不同 workflow_id 返回同样的全量 run），catchup 每次都把 keepalive/catchup 自身的成功运行误当成"主发送已成功"，导致**永远跳过、从不兜底**。修复：改用专用端点 `/actions/workflows/schedule.yml/runs`，并把 created_at（UTC）换算成北京日期再与当天比较（主发送在北京凌晨 = UTC 前一天，字符串前缀比较会跨天误判）。
11. **catchup 的 `gh workflow run` 必须显式 `--repo`（2026-08-26 暴露、08-27 修复）**：catchup 任务没有 checkout 步骤，工作目录不是 git 仓库，gh 无法从 remote 推断仓库 → `fatal: not a git repository` → dispatch 失败。此前该 bug 从未暴露，因为 catchup 的兜底分支（§6.10 修复前）从未真正走到过。2026-08-26 cookies 失效首次真实触发兜底时暴露。修复：`gh workflow run "DouYin Spark Flow Schedule Run" --repo Luolingli/DouYinSparkFlow`。
12. **新会话硬化期：扫码后 2~6h 内美国访问会被风控杀死（2026-08-27 事故，高置信推断）**：08-27 当天 2 个新会话分别在美国访问 +1.5min/+2min 后死亡；历史 08-02 会话（首次美国访问 +26.4h）与 08-17 会话（≥+6.2h）均存活 8 天/数周。会话首次使用前 80 秒内功能正常（聊天界面可渲染），之后才被杀——符合"访问触发风控、延迟生效"。对策（已全部落地）：① 本地扫码脚本 `keepalive.js` 扫码成功后写仓库变量 `LAST_RESCAN_UTC`（UTC 时间戳）；② catchup 在硬化期（距标记 <6h）内拒绝 dispatch；③ 操作规则：**重新扫码后 6 小时内不要手动触发任何使用 cookies 的 GitHub 工作流**。注意 GitHub cron 漂移可能让 catchup 在非计划时刻触发（08-27 实测 20:50/23:13 触发，其中 23:13 一次 dispatch 杀死了 73 分钟前刚扫码的会话）——硬化期保护就是为此设计的。
   补充（08-28 实测）：① S5 会话首次美国访问在 +3.4min，连登录检查都没通过就被杀（S1/S2 的首次访问能通过登录检查、之后才死，机制细节不确定，安全线只有 ≥6.2h 有存活证据）；② **审批门禁交互**：改 schedule.yml 后工作流被隔离，cron 触发的 run 会排队（实测排队 86 分钟），期间 Approve 会让排队 run 立刻开跑并捕获当时的 cookies——所以**扫码后不要立刻 Approve/等待审批中的 run**，两者叠加等于扫码后立即美国访问；③ 安全扫码窗口：北京 **18:40 之后**扫码（保证次日 00:40 发送距扫码 ≥6h）；白天扫码则只能靠 catchup 硬化期保护兜底（当天 00:40 发送必然失败或杀会话，当天火花由保护期后的 catchup 漂移/兜底抢救，不保证）。

## 7. 关键选择器（抖音改版时首要检查点）

```
搜索框:          input[placeholder="搜索"]
搜索联系人项:     .SearchPanelitembox
搜索项标题:       .SearchPanelitemtitle
发消息按钮:       .SearchPanelitemchat_btn
会话列表项:       .conversationConversationItemwrapper
会话标题:         .conversationConversationItemtitle
会话列表容器:     .conversationConversationListwrapper
聊天输入框:       .messageEditorimChatEditorContainer
消息框:          .messageMessageBoxmessageBox
我发的消息:       .MessageItemTextisFromMe
消息文本:        .MessageItemTextbubbleTextContent
消息时间:        .MessageBoxTimetimeLayout
```

## 8. 验证方式

- 运行日志关键行：`任务完成，共发送 10 条消息，跳过 0 条`（每天应如此；N = TASKS 里 targets 总数）。
- Review 输出：`[review] 主任务结果: success，日志发送数: 10/10` / `检查通过`。
- 工件 `run-logs`：`logs/app.log` + `logs/screenshots/发送后-*.png`（每发一人一张）。
- 当天重复触发应全跳过（日志出现 `SKIP_ALL_ALREADY_SENT`），review 判通过。
- 常用命令：
  ```bash
  gh run list --repo Luolingli/DouYinSparkFlow --limit 10
  gh run view --repo Luolingli/DouYinSparkFlow <run_id> --log
  gh workflow run "DouYin Spark Flow Schedule Run" --repo Luolingli/DouYinSparkFlow   # 手动触发
  ```

## 9. 调试规范（用户明确要求）

- **调试/多轮测试只允许发给 `zjj00000010`（。。。）和 `84611333990`（Eve.）**，其他人不得参与测试。
- 操作方式：`gh variable set DEBUG_TARGETS --repo ... --body '["zjj00000010","84611333990"]'` + `DEBUG_BYPASS_DEDUP=1` → 触发发送 → **测试完必须清空这两个变量**。
- 不要在调试时手动触发生产发送（会造成真实好友收到重复消息）。
- 未经用户允许，不要对真实账号做多轮/大批量发送。

## 10. 注意事项与边界

- GitHub cron 延迟不可控：接受“北京凌晨 00:40 触发、01:10-01:40 送达”，catchup 兜底保证当天至少一次。
- SMTP 未配置：cookies 过期时无专属邮件，靠 GitHub 失败通知（工作流名可区分）。
- 搜索依赖昵称映射；个别目标映射缺失或搜索失败时会走滚动兜底，偶发“未找到”只告警不失败。
- cookies 是账号凭证：不回显、不打印、不写进日志/文档。
- 上游参考仓库：`2061360308/DouYinSparkFlow`（dev/api 分支有替代实现思路）。

## 11. 继续工作 Prompt（复制给下一个 agent）

把下面这段完整发给接手 agent：

```text
你在接手一个抖音火花自动续火项目（仓库 Luolingli/DouYinSparkFlow，工作目录 .../DouYinSparkFlow）。
项目由 GitHub Actions 每天给 10 个好友各发一条"今日火花"消息，目前稳定运行中。
动手前必须通读仓库根目录 AGENTS.md，重点：核心逻辑在 core/tasks.py；搜索直达+滚动兜底；
浏览器必须 timezone_id=Asia/Shanghai；当天去重基于聊天面板时间（北京时间解析）。

硬性约束：
1. 调试/测试发送只允许发给 zjj00000010 和 84611333990 两个人，禁止发给其他好友。
   方式：临时设置仓库变量 DEBUG_TARGETS=['zjj00000010','84611333990'] 和 DEBUG_BYPASS_DEDUP=1，
   触发后必须清空变量。
2. 不要修改 .github/workflows/schedule.yml，除非用户明确要求（修改会触发 GitHub 审批门禁）。
3. 不要打印/泄露 COOKIES_SAKURO_MAI 的值。
4. 不要对真实账号做未经用户确认的多轮/大批量发送；不要自行反复手动触发生产发送。

验证一个改动时：跑完看日志"任务完成，共发送 N 条消息，跳过 M 条"和 review 输出；
当天已发过的好友应被跳过（不会重复发）。

常用命令：gh run list/view、gh workflow run "DouYin Spark Flow Schedule Run"。
如果 cookies 失效（日志报"未登录"），告诉用户需要重新导出 cookies 或本地扫码续期。
开始前先向我（用户）确认当前要解决的问题，再动手。
```

---

*文档维护：任何改变行为的重要改动，请同步更新本文件。*

## 12. 抖音单会话规则（2026-08-17 实测确认，务必遵守）

- **一个抖音账号同一时间只有一个有效网页会话**：任何一次扫码登录都会签发新 sessionid，旧会话立即失效。
- 实测案例：ai-news-douyin 项目的 douyin web login 扫码后，本项目的聊天会话 cookies 全部失效（keepalive 也救不回来）。
- 本项目与 ai-news-douyin **共享同一份会话 cookies**：COOKIES_SAKURO_MAI（secret） ↔ ~/.config/douyin_keepalive/cookies.json ↔ ai-news-douyin 的 data/web_cookies.json。
- **任何一方重新扫码后，必须把新 cookies 同步到另外两处**（gh secret set COOKIES_SAKURO_MAI --env user-data --repo Luolingli/DouYinSparkFlow + 复制本地文件），否则另一方必挂。
- 本项目本地扫码（`keepalive.js`）成功后会**自动**写仓库变量 `LAST_RESCAN_UTC`（供 catchup 硬化期保护，见 §6.12）；若通过其他途径更新 cookies，需手动同步该标记（或直接删除该变量——缺失时 catchup 按原逻辑 dispatch）。
- **扫码后 6 小时内不要手动触发任何使用 cookies 的 GitHub 工作流**（新会话硬化期，见 §6.12）。
- 日常运行（keepalive 心跳、发布后回写）不会创建新会话，可放心共存；**不要**为了"保险"而主动重新扫码。
