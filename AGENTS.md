# AGENTS.md — astrbot_plugin_minecraft_queqiao_next

> 本项目遵循 [agent-templates](https://github.com/XTsat/agent-templates) 的 AstrBot 插件规范模板。
> 完整规范请阅读：https://github.com/XTsat/agent-templates/blob/main/templates/astrbot-plugin/AGENTS.md
>
> 本文件是任何 AI 编码代理进入本项目时的**必读**工作说明书，补充本项目特有的约定；
> 与模板一致的通用条款（命名、日志、注释、提交、文档纪律）不再重复，直接以模板为准。

---

## 1. 项目概览

**astrbot_plugin_minecraft_queqiao_next** 是 AstrBot 插件：通过[鹊桥（QueQiao）](https://github.com/17TheWord/QueQiao)
模组连接 Minecraft 服务器，实现群服消息互通、服务器管理与 AI 聊天。

> 本仓库是上游 [astrbot_plugin_minecraft_queqiao](https://github.com/XTsat/astrbot_plugin_minecraft_queqiao)（作者 XTsat）的
> AGPL-3.0 fork，自 2026-10-19 起标识**彻底分离**（一切标识符 / 前缀 / 数据目录均为 `*_next`）：
> 与上游可同时安装、数据互不共用；上游署名与许可证保持不变。

- 配置结构**参照** [astrbot_plugin_minecraft_adapter](https://github.com/railgun19457/astrbot_plugin_minecraft_adapter)
- 连接方式**替换**为鹊桥 V2 协议（WebSocket + `echo` 请求响应关联）
- 协议文档：https://github.com/17TheWord/queqiao-docs

### 与 adapter 的关键差异（改动前必须理解）

| 维度 | adapter | 本项目 |
|---|---|---|
| 传输协议 | 自家 MC 插件私有协议（`CONNECTION_ACK`/`CHAT_REQUEST` 等） | 鹊桥 V2（`{api,data,echo}` + 事件 `post_type`） |
| 连接寻址 | `host` + `port` + URL 上的 `token` | `server_name`≡鹊桥 `server_name`，走 `x-self-name` Header |
| 鉴权 | URL query token | `Authorization: Bearer <access_token>` Header |
| 连接方向 | 仅正向 | 正向 + 反向（`ws_mode`） |
| AI 聊天触发 | 私有 `CHAT_REQUEST` 事件 | 聊天前缀触发（`ai_chat_prefix`，无斜杠指令） |
| 玩家详情 | REST 接口 | **无对应接口**，降级为 RCON `data get` |
| 状态查询 | REST | `get_status`（需鹊桥 ≥ v0.5.0） |

---

## 2. 目录结构与架构

采用**分层多文件**风格（模板 §2.2），依赖方向严格单向：

```
astrbot_plugin_minecraft_queqiao_next/
├── main.py                 # 插件入口：注册、命令组、生命周期、事件编排
├── core/                   # 核心层：配置模型 + 协议模型 + 连接管理
│   ├── constants.py        #   常量（协议标识/默认值/重连参数）—— 零内部依赖
│   ├── models.py           #   鹊桥事件与实体模型（防御式 from_dict）
│   ├── session_router.py   #   会话 UMO 解析/匹配/去重（隔离对话兼容）
│   ├── models_config.py    #   ServerConfig（对齐 _conf_schema.json）
│   ├── queqiao_client.py   #   WS 客户端：正/反向、握手、echo 关联、重连
│   ├── rcon_client.py      #   直连 RCON 兜底（惰性 import aiomcrcon）
│   └── server_manager.py   #   多服务器实例与指令执行（鹊桥优先/RCON 兜底）
├── services/               # 服务层
│   ├── binding.py          #   账号↔游戏ID 绑定（原子写）
│   ├── message_bridge.py   #   双向转发 + 回环抑制 + 富文本剥离
│   ├── image_host.py       #   内置图片 HTTP 服务（无公开 URL 图片转玩家可访问链接）
│   ├── image_bed.py        #   图片转存条目（内置 HTTP + 第三方图床统一接口）
│   ├── map_service.py      #   方块地图事件消费/持久化/快照合成（QueQiao-Map-Web）
│   ├── web_api.py          #   仪表盘 Web API（servers/terminal/monitor/map/panel…）
│   └── renderer.py         #   状态/玩家列表文本渲染
├── handlers/
│   └── commands.py         #   命令业务逻辑 + 自定义指令 + 多服务器选择
├── tests_offline.py        # 离线逻辑自检（桩替代 astrbot/websockets）
├── metadata.yaml / _conf_schema.json / README.md / README_en.md / CHANGELOG.md / LICENSE
```

依赖方向：`main.py → handlers / services → core`，`core/constants.py` 最底层。

---

## 3. 鹊桥协议约定（改动连接层前必读）

### 3.1 握手 Header

| Header | 必填 | 说明 |
|---|---|---|
| `x-self-name` | ✅ | 取 `server_name`，**必须与鹊桥 config.yml 的 `server_name` 一致** |
| `Authorization` | 选填 | `Bearer <access_token>`，token 为空时不发送 |
| `x-client-origin` | 建议 | 取 `client_origin`（默认 `astrbot`），用于防重复连接与自环 |

### 3.2 请求 / 响应

```json
// 请求
{"api": "broadcast", "data": {"message": [{"text": "hi", "color": "white"}]}, "echo": "<uuid>"}
// 响应
{"code": 200, "api": "broadcast", "post_type": "response", "status": "SUCCESS", "echo": "<uuid>"}
```

- **`call_api()` 必须在发送前登记 waiter**，否则鹊桥的快响应会因找不到 `echo` 被丢弃
- 消息内容一律是 **Minecraft 文本组件**（V2 不再手动解析 JSON）

### 3.3 事件

`post_type` 为 `message` 或 `notice` 时按事件处理；`response` 走 echo 关联。
事件名常量集中在 `core/constants.py`，**禁止在业务代码里硬编码事件名字符串**。

### 3.4 版本门槛（写进文档时必须如实标注）

| 能力 | 最低鹊桥版本 |
|---|---|
| API V2（broadcast/私聊/title/actionbar/rcon） | v0.2.11 |
| 事件 V2 | v0.3.0 |
| `Translate` 模型（死亡/成就文本） | v0.4.1 |
| `get_status` | v0.5.0 |

### 3.5 服务端差异（解析必须容忍）

- 原版端 `Player` 仅 `nickname`；Velocity 仅 `nickname`/`uuid`/`is_op`
- Spigot/Paper/Folia 无 `max_health`；Folia 可能缺 `address`
- **死亡 / 成就 / 命令事件在原版端与 Velocity 完全不支持**
- `raw_message` 在非原版端可能是 JSON 文本组件 → 转发前必须 `component_to_text()` 剥离

---

## 4. 本项目特有约定

### 4.1 新增配置项

1. 改 `_conf_schema.json`（**真实扁平结构**，不是模板示意里的 `schemaVersion/fields`）
2. 改 `core/models_config.py` 的 `ServerConfig` 字段 + `from_dict()` 兜底解析
3. 在业务代码读取时保持防御式（WebUI 值可能是字符串）
4. 更新 `README.md` 与 `README_en.md` 的配置表

### 4.2 AstrBot API 导入路径（写错会导致插件加载失败）

**不要凭记忆猜导入路径**，以下均已对照真实 AstrBot 源码核实：

| 符号 | 正确来源 | 备注 |
|---|---|---|
| `AstrBotConfig` | `astrbot.api` | |
| `logger` | `astrbot.api` | 会按调用方模块自动路由到插件专属 logger |
| `AstrMessageEvent` | `astrbot.api.event` | |
| `filter` | `astrbot.api.event` | 是**子包**，不是 `__init__` 里的名字，但可导入 |
| `Context` | **`astrbot.api.star`** | ⚠️ **`astrbot.api` 没有它** |
| `Star` / `StarTools` / `register` | `astrbot.api.star` | |
| `GreedyStr` | `astrbot.core.star.filter.command` | |

> 已知踩坑：曾写成 `from astrbot.api import Context`，导致
> `cannot import name 'Context' from 'astrbot.api'` 插件加载失败。
>
> `tests_offline.py` 的桩**必须忠实反映真实 API**（尤其 `astrbot.api` 不得提供
> `Context`），否则桩会掩盖此类错误——第 16 组断言专门守卫这一点。
>
> 另注：`LLMResponse.completion_text` 已被官方标记「过时，推荐 `result_chain`」，
> 目前仍可用；若未来移除需改 `_ask_llm()`。
>
> 核实方式：真实 AstrBot 源码在 `/vol2/@appdata/deepseek.harness/home/astrbot/astrbot/`，
> 兄弟插件在 `/vol2/@appdata/deepseek.harness/home/astrbot_plugin/`。

### 4.3 日志前缀

统一 `[{PLUGIN_NAME}][{server_name}]` 二级前缀，便于多服务器排障。

### 4.4 连接层约束

- 反向模式下**同端口多服务器共享一个 WS Server**（`SharedReverseServer`），按 `x-self-name` 路由；不要每台服务器各起一个 listener
- 致命错误不重试：关闭码 `{1003,1008,1010}`、HTTP `{401,403,404}`
- 重连等待 `min(reconnect_interval × retries, 60)`
- 连接断开时必须 `_fail_pending()`，否则调用方永久挂起

### 4.5 指令通道

`ServerInstance.execute_command()` 的优先级：**鹊桥 `send_rcon_command` → 直连 RCON 兜底**。
两条通道都不可用时返回 `None`（调用方据此给出可操作的提示，而非静默失败）。

**超时 ≠ 失败**（改回执/兜底逻辑前必读）：鹊桥 WS 发送成功但 `API_TIMEOUT`
内无响应时抛 `QueQiaoTimeout`——请求大概率已投递执行，只是响应慢/丢失。
此时**禁止**换通道重发（消息/指令会重复两遍，实测发生过 AI 回复发两遍）：
- 私聊回复（`send_private_message`）与 RCON（`send_rcon_command`）超时向上抛，
  由调用方放弃本次发送，不得自动兜底重发
- 展示类（broadcast / title / actionbar）超时在封装层按已投递处理返回 True
- 仅「确定失败」（未连接 / 鹊桥明确报错）才允许走兜底通道

### 4.6 AI 触发方式与两个前缀（不可混淆）

游戏内 AI **仅由聊天前缀触发**（`PlayerChatEvent` + `ai_chat_prefix`，默认 `ai`），全端可用。

> **明确不做斜杠触发**：本插件运行在 AstrBot 侧，无法向 Minecraft 注册真实指令。
> 且斜杠输入在游戏内属于**指令**而非聊天：Fabric 侧 `onChatMessage` 直接
> `if (message.startsWith("/")) return;`，Spigot 侧走 `PlayerCommandPreprocessEvent`
> 而不进 `AsyncPlayerChatEvent`。因此 `/ai 你好` **既不会触发 AI，也不会转发到群**
> ——它压根不会进入聊天事件。玩家只会看到原版 `Unknown command` 报错。
> 不要再引入 `ai_command_*` 配置或 `PlayerCommandEvent` 触发路径；
> 若确实需要原生指令，应由服务端侧插件提供。

前缀语义：

| 配置项 | 方向 | 默认值 | 语义 |
|---|---|---|---|
| `auto_forward_prefix` | 群 → MC | 留空 | 群消息以此开头才转发到游戏；留空 = 全部转发（仅对已绑定 `target_sessions` 的群生效） |
| `ai_chat_prefix` | 游戏内 → AI | `ai` | 游戏内聊天以此开头才触发 AI；留空 = 不触发 |

不变式（改事件编排时必须保持）：
- 游戏内一条消息**只命中一条路径**：先调 `_resolve_ai_question()`，返回非 None 即交给 LLM 并 return，不再转发
- `ai_chat_prefix` 留空**不得**退化为「全部触发」——那会把全部聊天投给 LLM
- 两个前缀互相包含时 `ServerConfig.prefixes_conflict` 为真，启动时告警
- 指令事件（`PlayerCommandEvent`）**不得**进入 AI 分支，应放行给互通流程
- 触发后内容为空（`ai`）不得向 LLM 发请求
- 前缀匹配一律走 `core/constants.py` 的 `prefix_matches()` / `strip_prefix()`，
  **禁止直接用 `str.startswith`**：短前缀（`ai`）必须做词边界判断，
  否则 `aim`、`airport` 会被误判；纯字母前缀忽略大小写

### 4.7 前端页面持久化约束（改 pages/ 前必读）

AstrBot 插件页面运行在**沙箱 iframe**（`dashboard/src/views/PluginPagePage.vue`：
`sandbox="allow-scripts allow-forms allow-downloads"`，**没有 `allow-same-origin`**）。
这种沙箱下访问 `localStorage` / `sessionStorage` / `document.cookie` 会**直接抛
SecurityError**，不是返回空——任何「try/catch 包一下就能用」的假设都无效
（实测：tab 记忆写不进也读不出，静默回退默认值）。

因此：

- **前端任何需要「跨刷新 / 跨设备保持」的状态，权威值一律走后端落盘**
  （本项目用 `panel_prefs.json` + `/panel/prefs` API，见
  `services/web_api.py::set_panel_prefs`：白名单字段、钳制校验、合并原子落盘）
- 已按此模式落盘的偏好：`active_tab`（当前服务器视图）、`auto_refresh`
  （自动刷新开关）、`auto_refresh_interval`（间隔秒数）、
  `settings_collapsed`（功能设置面板折叠）
- 例外：`terminal_days`（互通终端加载天数）早期也走 panel_prefs，后迁为
  **每台服务器独立配置**（conf 条目顶层 + `SOFT_CONFIG_KEYS` 软更新 +
  `/config/server/update`），前端按当前服务器读取；`set_panel_prefs` 仍保留
  该字段仅作旧客户端兼容，前端不再写入
- 浏览器存储最多作「启动缓存」：访问必须 try/catch，加载后以异步拉回的后端
  值为准覆盖；**不得**把用户可见状态只存在浏览器
- 新增需持久化的前端状态时：先在后端 `set_panel_prefs` 加白名单字段（含
  类型/长度校验与钳制），再在前端异步写 + 异步恢复；不要在 iframe 里依赖
  任何本地存储

### 4.8 目标会话匹配与转发去重（改 on_message / MessageBridge 前必读）

`target_sessions` 的匹配**不得再按整串 UMO 精确比较**，统一走
`core/session_router.py`（`parse_session_entry` / `event_session_ref` /
`session_matches` / `send_target_key`）：

- AstrBot 开启「隔离对话」（`platform_settings.unique_session`）后，群消息 UMO
  会被改写成 `平台:GroupMessage:发送者ID_群ID`，**同一群每个成员的 UMO 都不同**。
  事件侧真实群号取 `event.get_group_id()`；配置侧复合 ID 只在
  `纯数字_纯数字` 形态下才拆群号（避免把 Lark `oc_xxx` 这类真实会话 ID 拆坏）。
- 匹配入口是 `MessageBridge.servers_for_event(event)`（带事件）；无事件对象的
  场景（测试 / 外部调用）才用 `servers_for_session(umo)` 退化接口。
- MC → 外部发送前必须经 `MessageBridge.resolve_send_targets(config)` 去重：
  同群多条 UMO（隔离对话下每人一条、或重复粘贴）只发一次；完整 UMO **原样发送**，
  只有短写法（`平台:群号` / 裸群号）才用事件里学到的规范 UMO 回填。
- 外部消息进 `on_message` 后依次拦截：Bot 自身消息（`self_id == sender_id`）→
  重复投递（`(平台, message_id)` 短窗口幂等）→ MC → 群刚发出又被平台回传的内容
  （`is_outbound_echo`，按会话去重键 + 短窗口）。命中任一条都不得转发进 MC。
- `apply_soft_config()` 改到 `target_sessions` 时必须调用
  `message_bridge.reregister_server()` 重建索引，否则群 → MC 方向仍按旧会话匹配。
- `/mc target`（add / remove）由 main 层直接写 conf 条目（`save_config_async`），
  绑定值取 `event_session_ref(event).canonical`——群消息收敛成群号，避免把
  发送者 ID 写进配置。

### 4.9 方块地图（改 pages/dashboard/map-view.js 或 map_service.py 前必读）

数据规范以 QueQiao-Map-Web 的 `docs/INTEGRATION.md` 为准，四类事件按 `sub_type` 路由：
`map_dimensions` / `map_palette` / `map_tile`（`zlib-pal8` + `sha1` 去重）/ `player_move`。

后端不变式：

- 维度映射：内置 `MAP_DIMENSIONS` 之外的 registry key 走 `_dim_short()` 动态登记，
  短名随 `meta.json` 的 `dyn_dims` 持久化；非法 key 一律不入库（`MAP_REGISTRY_KEY_RE`）
- 调色板 **id 变化或颜色内容变化**都要清空该服瓦片并 bump revision（否则新旧量化混用出错色）
- 合成快照时把调色板里下标 > 0 的**纯黑**抬成 `(0, 0, 1)`（`_BLACK_FIX`）：纯黑在快照里
  是「未探索」占位色，不抬就会被画成暗色遮罩
- 快照必须带 `revision`（无数据态恒 0），前端靠它做「未变化即跳过重解码」的指纹
- 瓦片/调色板持久化到 `data_dir/map/<安全服务器名>/`，节流 ≥5s 落盘，`terminate` 时 flush

前端（闪烁禁忌，都是踩过的坑）：

- **画布尺寸绝不能抖动**：`.map-canvas-wrap` 是 `flex:1`，任何兄弟元素高度变化都会改变
  画布位图尺寸并触发重绘（肉眼 = 地图跳一下 / 闪一下）。因此 `.map-status-row` 是
  **定高 32px + nowrap + overflow:hidden**，CSS 守卫在 `tests_offline.py` 组 52；
  新增状态行/工具行元素时必须定高，长文案用省略号
- 解码走离屏 canvas，成功后一次性换入 `state.tileCanvas`；解码失败或空瓦片时**保留旧帧**，
  不得 `clearRect` 清屏、不得把空瓦片写成红色错误状态行
- 快照指纹优先用 `revision`；轮询 / 同值文本（状态行、数据源徽标、更新时间）一律幂等写入
- 同一服务器视图重复激活（`renderServerTabs` 每 tick 调 `activateServerTab`）不得重启轮询定时器
- 维度下拉由 `status.dimensions` + `status.dimension_names` 驱动（`applyDimensions()`），
  清单未变化时零 DOM 操作；当前维度被移除时回落主世界
- 玩家定位下拉与维度下拉同款「签名化零重建」（数据源 `GET map/<server>/players` 跨维度清单，按 name 排序保证签名稳定；开关与选项文案必须定宽 + 省略号——标签变宽撑换行工具栏同样会抖画布）

### 4.10 自检

改动纯逻辑后运行 `python3 tests_offline.py`（55 组断言，覆盖配置解析、事件模型、
转发/回声抑制、自定义指令、绑定持久化、AI 触发方式与前缀互斥语义、
端到端事件流、main 导入、AstrBot 导入路径校验、显示名称与格式默认值、
conf 模板↔代码默认值一致性守卫、API 超时语义「未知 ≠ 失败，禁止重发」、
会话 UMO 归一/隔离对话同群全成员匹配、目标会话去重、自消息与重复投递拦截、
`mc target` 免 UMO 绑定、方块地图无数据契约与 API、zlib-pal8 解码/sha1 去重/
调色板换 id 与换色清瓦片/纯黑夹紧/快照 revision/模组维度动态登记与持久化、
地图事件旁路消费与 `map_enabled` 门控、前端动态维度与状态行定高守卫、
方块地图跨维度玩家清单 `players()`（仅 player_move 可定位 / 新鲜度 / 禁用空态 / Web API 冒烟）。

---

## 5. 文档维护（强制）

- `README.md` + `README_en.md` 必须同步；头部格式与四大板块结构遵循模板 §7.2（中文为主、单向同步：先落中文版再同步英文版；中文 `h1` 取 `metadata.yaml` 的 `display_name`）
- `CHANGELOG.md` 顶部固定 `[Unreleased]`，**分组式**（与模板 §7.1 一致）：每个版本段下用 `### 新增` / `### 修复` / `### 变更` / `### 移除` / `### 性能` 子标题分组，只保留有内容的分类；条目采用「**加粗主题**：说明」形式
- 版本号三处联动：`metadata.yaml` ↔ `CHANGELOG.md` 最新版本 ↔ README 徽章
- 未同步文档 = 任务未完成

---

## 6. 提交约定

- commit 用中文短句式；**提交前必须先向用户展示改动内容与提交标题，等确认后再执行**
- 只有用户明确要求时才 commit / push

---

## 7. 最终检查清单

- [ ] 新增/修改配置项已在 `_conf_schema.json` 与 `core/models_config.py` 同步
- [ ] 事件名/API 名走 `core/constants.py` 常量，未硬编码
- [ ] 日志带 `[{PLUGIN_NAME}][{server_name}]` 前缀，无空 `except: pass`
- [ ] 连接层改动已考虑反向模式共享 Server 与 echo 关联时序
- [ ] 目标会话匹配/去重改动已走 `core/session_router.py`，并覆盖隔离对话场景
- [ ] `python3 tests_offline.py` 通过
- [ ] `README.md` / `README_en.md` / `CHANGELOG.md` 已同步，版本号一致
