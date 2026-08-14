# DouYinSparkFlow 项目交接文档

> 本文件供任何接手此项目的 agent/开发者完整理解项目。请先通读，再动手。
> 最后更新时间：2026-08-14（main 分支，最新提交 `698d653`）

## 1. 项目是什么

**抖音火花自动续火脚本**：每天自动通过抖音网页版聊天页（`https://www.douyin.com/chat`）给指定好友各发送一条“今日火花”消息，维持好友火花。由 GitHub Actions 定时运行，无人值守。

- 账号：`Sakuro_Mai`（抖音昵称 Sakuro.）
- 目标好友：9 人（见 §4 TASKS）
- 运行环境：GitHub Actions（ubuntu-latest），云端无头 Chromium

## 2. 当前状态（截至 2026-08-14）

- **稳定运行中**：08-09 ~ 08-14 每天 9/9 全发、每人恰好 1 条、无漏发无重复（核对方法见 §8）。
- 发送时间：cron `0 19 * * *`（UTC）= 北京时间次日 03:00；GitHub 延迟后实际约 **北京 03:30~04:00** 送达。
- cookies 由 keepalive 每 6 小时自动续期回写（密钥最后更新时间即最近一次续期）。

## 3. 架构与文件

### 工作流（.github/workflows/）

| 文件 | 作用 | cron（UTC） | 备注 |
|---|---|---|---|
| `schedule.yml` | **主发送** | `0 19 * * *` | ⚠️ **修改此文件会触发 GitHub“恶意工作流”审批门禁**，需要仓库所有者手动 Approve；非必要不要改 |
| `catchup.yml` | 兜底触发 | `17 2 / 17 4 / 17 7 * * *` | 查当天是否已有成功发送（`gh api runs?status=success`），没有才 dispatch 主发送；新文件可安全修改 |
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
  [{"username":"Sakuro.","unique_id":"Sakuro_Mai","targets":["84611333990","57569913835","HOLLOW_LOVE","zjj00000010","25191158994","xiaolangaini","1191371127","heihahou7316","98241180006"]},{"username":"","unique_id":"","targets":[]}]
  ```
  注意：第二个空任务（无 unique_id）会被跳过并打一条 WARNING，可清理但无害。
- `MATCH_MODE=short_id`：匹配用抖音号/short_id（脚本实际同时支持多种标识符，见 §5）
- `MESSAGE_TEMPLATE`：`[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]`
- `HITOKOTO_TYPES=["文学","诗词","哲学"]`，`LOG_LEVEL=Debug`
- `BROWSER_TIMEOUT=120000`、`FRIEND_LIST_WAIT_TIME=2000`、`TASK_RETRY_TIMES=3`

### 仓库 Variables（临时调试用）

- `DEBUG_TARGETS`：JSON 数组，如 `["zjj00000010","84611333990"]`；设置后只发给这些目标。
- `DEBUG_BYPASS_DEDUP=1`：调试时绕过当天去重，允许多轮测试。
- **用完必须清空**（`gh variable delete`），否则生产运行也会被限制。
- `LAST_SEND_DATE`：历史遗留变量（旧去重方案），当前代码不使用，可删除。

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
7. **GitHub cron 延迟/丢失**：cron 可能延迟 3-5 小时甚至跳过（官方特性）。对策：主 cron 提前（北京 03:00）+ catchup 多时段兜底 + 应用层幂等。**不要承诺“准时 9 点”**。
8. **工作流审批门禁**：改 `schedule.yml` 会触发 GitHub“potentially malicious workflow”审批；`gh run rerun`、`gh variable set` 等自管理步骤同样触发。**新工作流文件（catchup/review/keepalive）不受影响，可安全修改。**
9. **review 误报**：0 发送但已有跳过（当天已发）曾误判失败 → 已改为“跳过>0 不算失败”。

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

- 运行日志关键行：`任务完成，共发送 9 条消息，跳过 0 条`（每天应如此）。
- Review 输出：`[review] 主任务结果: success，日志发送数: 9/9` / `检查通过`。
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

- GitHub cron 延迟不可控：接受“北京凌晨 3 点触发、3:30-4:00 送达”，catchup 兜底保证当天至少一次。
- SMTP 未配置：cookies 过期时无专属邮件，靠 GitHub 失败通知（工作流名可区分）。
- 搜索依赖昵称映射；个别目标映射缺失或搜索失败时会走滚动兜底，偶发“未找到”只告警不失败。
- cookies 是账号凭证：不回显、不打印、不写进日志/文档。
- 上游参考仓库：`2061360308/DouYinSparkFlow`（dev/api 分支有替代实现思路）。

## 11. 继续工作 Prompt（复制给下一个 agent）

把下面这段完整发给接手 agent：

```text
你在接手一个抖音火花自动续火项目（仓库 Luolingli/DouYinSparkFlow，工作目录 .../DouYinSparkFlow）。
项目由 GitHub Actions 每天给 9 个好友各发一条"今日火花"消息，目前稳定运行中。
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
