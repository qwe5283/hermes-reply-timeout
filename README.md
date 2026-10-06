# reply-timeout (Hermes plugin)

Hermes Agent 插件：为飞书私聊会话提供「回复超时」机制。

## 解决什么问题

Hermes 的交互提问（clarify）只能活在一轮对话内；cron 是定时器不是等回信；heartbeat
是会话级且只能用户手动挂。插件补上「我留了一条需要你回复的消息，超时后我自己决定
下一步」这个缺口。

## 行为（P0：仅飞书私聊）

1. 每轮最终回复落地后（`post_llm_call`），后台线程发起**一次无状态**结构化意图调用
   （`ctx.llm.complete_structured`，不进会话上下文、不写 state.db 对话史）：输出整数
   分钟数——等用户回复的预计时长，0＝不等待，钳制在 `[min_minutes, max_minutes]`
   （默认 1–120）。
2. 分钟数 > 0 → 为该会话挂定时器（每会话唯一，新挂替换旧挂），并向会话输出
   「【回复超时 N min 后触发】」（可关）。
3. 超时窗口内该会话收到**真实用户消息**即取消定时器并重置链计数；插件自己注入的
   `<system_reminder>` 与 `[Cron delivery:` 镜像不算用户消息，不会误取消/误重置。
4. 到时未回 → 向会话注入一条 user 角色 `<system_reminder>用户在 N min 后未做回复
   </system_reminder>`（有状态：进上下文、写 state.db、触发完整一轮 agent 运行），
   由 Hermes 依据上下文自行决策。
5. 唤醒轮自身允许再挂超时，但链长 ≤ `max_chain`（默认 3）；真实用户消息重置计数。

群聊（`group_sessions_per_user`、话题）与 cron 投递适配为第二版范围。

## 行为流程图

```mermaid
flowchart TD
    A["最终回复落地<br/>（post_llm_call · 仅飞书私聊会话）"] --> B["后台线程发起无状态意图调用<br/>ctx.llm.complete_structured<br/>（不进上下文、不写对话史）"]
    B --> C{"minutes = 0？<br/>（0＝不等待，非零钳制 1–120）"}
    C -- "0" --> X1["不挂起，本轮结束"]
    C -- "N &gt; 0" --> D["挂 per-chat 定时器（落盘可恢复）<br/>输出「【回复超时 N min 后触发】」<br/>（announce 开关，默认开）"]
    D --> E{"超时窗口内发生什么？"}
    E -- "真实用户消息<br/>（pre_llm_call）" --> F["取消定时器<br/>重置链计数 → 回到正常对话"]
    E -- "/new 或 /reset（pre_command / on_session_reset）" --> F2["取消定时器＋清除链记录<br/>（上下文压缩不取消）"]
    E -- "系统消息<br/>（system_reminder 自注入 / Cron 投递镜像）" --> G["识别并忽略<br/>不影响计时"]
    E -- "到时未回" --> H["注入提醒消息<br/>「用户在 N min 后未做回复」<br/>（user 角色 · 进上下文 · 触发完整 agent 回合）"]
    H --> I{"链长 &lt; max_chain？<br/>（默认 3）"}
    I -- "是" --> B
    I -- "否" --> X2["达链上限，不再挂起<br/>（等待真实用户消息重置）"]
```

## 任务清单

- [x] P0 实现（v0.1.0 · 2026-10-06）
  - [x] `post_llm_call` 挂起 + 无状态意图调用（`ctx.llm.complete_structured`）
  - [x] per-chat 定时器 + 横幅输出（`announce` 开关）
  - [x] 超时注入 `system_reminder`（`allow_gateway_injection: true`）
  - [x] 真实入站取消并重置链；系统消息过滤（reminder / Cron 镜像不误触发）
  - [x] 链上限（`max_chain`，默认 3）
  - [x] 网关重启恢复（未到期按剩余时间重挂、已到期丢弃）
  - [x] 专用意图模型配置（`intent_provider` / `intent_model`，未授权时回退会话模型）
  - [x] 离线单测 22 项全通过（FakeCtx / FakeLlm）
  - [x] 10-06 实测修复：`hermes send` 子进程级联（register 副作用限网关进程＋restore 永不 announce＋直连飞书 API 发横幅）与同进程双注册守卫（单测 28 项）
  - [x] 10-06 P0 阻断修复：计时器创建后漏 `start()` 致到点不触发；补「真实到点触发」防回归用例（单测 30 项）
  - [x] 10-06 线上验证通过：单横幅 ✓ 挂起 ✓ 真实到点注入 ✓ 入站取消重置 ✓（15:40 实录）
  - [x] 10-06 评审落地：`/new` `/reset` 取消计时器并清链记录（`pre_command` 主路径＋`on_session_reset` 兜底；上下文压缩不取消）（v0.1.1 · 单测 37 项）
  - [x] 10-06 线上验证：链递增 1→2→3 与 `chain cap reached; not arming` 自然到达（16:31 实录）；`/new` 取消（16:16 实录；`/reset` 与 `/new` 同一命令定义免测）
- [x] 横幅撤回（v0.1.2 · 2026-10-06 · 单测 52 项）
  - [x] `_feishu_send_text` 返回 `message_id`，存入 rec 随计时器落盘（重启后仍可撤）
  - [x] 四条消亡路径全撤：入站取消 / 到点触发 / 同会话替换 / unload
  - [x] 开关：插件 `settings.cleanup_progress` > `display.platforms.feishu.cleanup_progress` > `display.cleanup_progress` > 默认关（display 经 `load_config_readonly()` 进程内读取）
  - [x] 撤回 `DELETE /open-apis/im/v1/messages/:id`；兜底路径横幅无 id 跳过；失败仅 warning
- [x] 横幅撤回线上验证（v0.1.2 已于 10-06 17:30 随网关重启上线；`display.cleanup_progress: true`）
  - [x] 入站取消路径：17:32 实录 `recalled banner om_x100b637… (real inbound)`，用户肉眼确认横幅消失
  - [x] 到点触发（fire）路径：17:40 实录 `recalled banner om_x100b637… (timer fired)`，注入提醒后横幅同步撤回
- [ ] cron 适配（P1 / v0.2）
  - [ ] 显式 `arm` 工具：cron 提示词可调用「挂 N 分钟回复超时」
  - [ ] cron 投递回合的挂起与回复解除（晨间对齐 / 22:30 日报接入）
- [ ] 群聊适配（P2 / v0.3）
  - [ ] `group_sessions_per_user` 两种隔离模式的键位适配
  - [ ] 话题群（thread）会话键支持与投递落点验证
  - [ ] feishu-history 插件兼容性验证
- [ ] 礼貌守卫（非功能性 / P3）
  - [ ] 静默时段（active hours，窗外不挂或顺延）
  - [ ] 次数/频率上限（每日唤醒配额）

## 配置（`~/.hermes/config.yaml`）

```yaml
plugins:
  enabled:
    - reply-timeout
  entries:
    reply-timeout:
      allow_gateway_injection: true   # 必需：允许注入提醒消息触发网关回合
      settings:
        announce: true                # 是否输出「【回复超时 N min 后触发】」
        cleanup_progress: true        # 撤回已消亡计时器的横幅；不设则镜像 display 设置
        max_chain: 3                  # 唤醒链上限
        min_minutes: 1                # 钳制下限
        max_minutes: 120              # 钳制上限
        intent_provider: ""           # 专用意图模型 provider（空＝继承会话）
        intent_model: ""              # 专用意图模型名（空＝继承会话）
```

> 专用模型覆盖需要同时开 `llm.allow_model_override`（跨 provider 还需
> `llm.allow_provider_override`），否则回退到会话模型——见官方文档 Plugin LLM Access。

**横幅撤回开关（v0.1.2）**：优先级＝插件 `settings.cleanup_progress`（显式设置时覆盖）
> `display.platforms.feishu.cleanup_progress` > `display.cleanup_progress` > 默认关。
> display 读取走核心进程内 `load_config_readonly()`（不解析 yaml 文件）。计时器消亡
> 四路径（真实入站取消 / 到点触发 / 新计时器替换 / unload）都会撤回横幅；直连 API
> 发送的横幅带 `message_id` 可撤，`hermes send` 兜底路径发出的无法撤（记 debug 跳过）。
> 撤回走 `DELETE /open-apis/im/v1/messages/:message_id`，失败仅 warning 不影响主流程。

生效需重启网关（用户手动执行）。

## 设计要点

- 意图调用复用当轮上下文要点（用户消息＋最终回复，各截 1200 字）缓存命中不做承诺（前缀逐字节一致才能命中）。
- 计时器经 `ctx.state` 落盘：网关重启后未到期的按剩余时间重挂，已到期的丢弃。
- `state.db` 只以 `mode=ro` 打开（本机铁律，防 store-lockout）。
- 提醒注入走官方 `ctx.inject_message`（网关模式需 `allow_gateway_injection: true`）；
  横幅经 `hermes send` 子进程发送（插件无裸发消息 API，且不 import 适配器内部）。
- 钩子回调立即返回（30s 上限约束）：意图调用＋挂表全部在受监管后台线程完成。

## 验证

```bash
cd ~/.hermes/plugins/reply-timeout
python3 tests/test_offline.py   # 离线单测（不调 LLM、不真发消息）
```

## License

MIT
