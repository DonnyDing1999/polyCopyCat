# Discord 自建服务：从开发到部署完整指南

> 以 `server-bot` 为范本，讲清「在 Discord 部署属于自己的服务」这件事的全流程。
> 适用对象：想在这套架构上继续加功能、或照葫芦画瓢再建一个新服务的人。
> 范例服务：`server-bot`（Cloudflare Worker，命令 `/成员列表`），线上地址
> `https://server-bot.tjwangbusiness.workers.dev`，Guild ID `1525740861000777778`。

---

## 目录

1. [核心心智模型](#1-核心心智模型)
2. [两种架构：被动应答 vs 常驻在线](#2-两种架构被动应答-vs-常驻在线)
3. [一次交互的完整生命周期](#3-一次交互的完整生命周期)
4. [项目架构：为什么这样分文件](#4-项目架构为什么这样分文件)
5. [开发一个新命令（三步）](#5-开发一个新命令三步)
6. [部署全流程（首次）](#6-部署全流程首次)
7. [日常运维](#7-日常运维)
8. [排错手册（含本次踩过的坑）](#8-排错手册含本次踩过的坑)
9. [安全约定](#9-安全约定)

---

## 1. 核心心智模型

Discord 上「你自己的服务」= **一个注册在 Discord 的应用（Application）+ 一段你自己的代码**。

- Discord 只提供「舞台」：服务器、频道、用户、消息、表单这些 UI。
- 你的代码**不住在 Discord 里**，而是跑在别处（这里是 Cloudflare 边缘节点）。
- 两者靠网络对话。要搞懂一切，只需回答一个问题：
  **你的代码怎么「听到」用户在 Discord 里做了什么，又怎么「让」Discord 显示东西？**

一个完整服务其实是**三件东西各司其职**：

```
register.js  →  一次性告诉 Discord「我有哪些命令」        （登记菜单）
Worker 代码  →  每次有人用命令时「怎么应答」              （后厨干活）
Endpoint URL →  告诉 Discord「干活的后厨在哪个网址」      （留地址）
```

---

## 2. 两种架构：被动应答 vs 常驻在线

### 方式 A：HTTP Interactions —— 被动应答（`server-bot` / `trade-bot` 用这个）

```
用户在 Discord 打 /成员列表
        │
        ▼
 Discord 服务器  ──把交互打包成 POST──►  你的 Worker
        ◄──────────Worker 回 JSON──────────
        │
        ▼
 Discord 照 JSON 弹表单 / 发消息
```

- 像一家餐厅：有客人（POST）才开门应答，没客人就睡觉，**不占资源**。
- 你只需给 Discord 一个**网址（Interactions Endpoint URL）**。
- 只能处理「用户主动发起的交互」：斜杠命令、按钮、下拉菜单、表单。
  **听不到普通聊天，也不能主动定时推送。**
- 好处：不用 7×24 常驻、免费额度足够、零运维。

### 方式 B：Gateway —— 常驻在线（`discord-mcp` 用这个）

```
你的代码 ══ 一条永不挂断的 WebSocket 长连接 ══ Discord
```

- 像坐在群里的真人：一直在线，**所有消息都能看到**，能主动发言、定时播报。
- 代价：必须有个进程一直跑着（Docker / 云服务器），关机就掉线。

> **一句话**：A 省心但只能被动应答；B 全能但要一直开机。
> 除非需要「监听聊天 / 主动定时推送」，否则一律选 A。

---

## 3. 一次交互的完整生命周期

以 `/成员列表` 为例（这是「延迟响应」模式，最完整）：

```
1. 用户打 /成员列表
2. Discord POST 到 Worker，带 Ed25519 签名
3. Worker：用 PUBLIC_KEY 验签 → 不通过就 401 拒绝
4. Worker：这是斜杠命令(type 2) → 路由到 members 命令
5. members 命令要调 REST 拉成员，可能 >3 秒，于是：
     - 立刻回一个「延迟应答」(type 5)，Discord 显示「思考中…」
     - 同时用 ctx.waitUntil 在后台异步干活（Worker 不会因返回而被杀）
6. 后台：用 Bot Token 调 Discord REST 拉全部成员 + 角色，排版
7. 后台：PATCH /webhooks/{app_id}/{token}/messages/@original
     把「思考中…」那条消息**回填**成最终名单
8. 用户看到结果（仅本人可见 / 或 .txt 附件）
```

**为什么要「延迟应答」？** Discord 要求你在 **3 秒内**给出第一次响应，否则判定交互失败。
调 REST 拉数据可能超 3 秒，所以先回「思考中」占住位置，再慢慢回填。
不需要调外部数据、能立刻算出结果的命令（如弹个表单）就不用延迟，直接回 type 4/9。

**关键点**：
- **验签**用 `DISCORD_PUBLIC_KEY`（公开值）。
- **主动调 REST**（拉成员）用 `DISCORD_TOKEN`（机密）。`trade-bot` 那种纯应答不需要 token。
- **回填**走 `interaction.token`，无需 bot 鉴权；且必须在 **15 分钟**内完成。

---

## 4. 项目架构：为什么这样分文件

```
src/
  index.js            主入口：验签 → 认握手 → 路由命令。稳定，不随命令增减而改
  discord.js          通用工具层：验签 / REST 调用 / 拉成员·角色 / 回填延迟消息
  commands/
    index.js          命令注册表（唯一的命令清单）
    members.js        /成员列表 命令
register.js           一次性把命令注册到 Discord（Node 跑，非 Worker）
wrangler.toml         Worker 配置（name / main / 兼容日期）
package.json          脚本：deploy / dev / register
.env(.example)        register.js 用的本地环境变量（含 Token，不入库）
```

**分层意图**：
- `index.js` 只做「分发」，加命令时**它不用改** → 入口稳定。
- `discord.js` 沉淀所有**跨命令复用**的能力（验签、REST、回填）→ 新命令直接调，不重写。
- `commands/*` 每个文件一个命令，**互不影响** → 改一个不碰别的。
- `commands/index.js` 是唯一「命令清单」，路由和注册都读它 → 加命令只动这一处 + 新文件。

一个命令模块的约定（照 `members.js`）：

| 导出 | 作用 |
|---|---|
| `definition` | 注册给 Discord 的元信息：`name` / `description` / `type` / 权限 |
| `deferred`（可选） | `true` = 需要「先应答再回填」（要调 REST、可能 >3 秒） |
| `run(interaction, env, ctx)` | 命令逻辑。`deferred` 时只负责用 `ctx.waitUntil` 安排异步活儿并立即返回；否则直接返回一个响应对象 |

---

## 5. 开发一个新命令（三步）

假设要加 `/在线人数`：

**① 新建 `src/commands/online.js`**（能立刻算出、不调 REST 的简单命令示例）

```js
import { RES, EPHEMERAL } from "../discord.js";

export const definition = {
  name: "在线人数",
  description: "显示当前在线人数",
  type: 1,
};

// 不需要延迟：直接返回一条消息
export async function run(interaction, env, ctx) {
  return {
    type: RES.CHANNEL_MESSAGE,
    data: { content: "（示例）当前在线：42 人", flags: EPHEMERAL },
  };
}
```

> 若命令要**调 Discord REST**（拉数据、踢人、发公告等），就照 `members.js`：
> 设 `export const deferred = true;`，在 `run` 里 `ctx.waitUntil(...)`，用 `discord.js`
> 里的 `discordREST` / `fetchAllMembers` 等，最后 `editOriginal(interaction, {...})` 回填。

**② 在 `src/commands/index.js` 注册进去**

```js
import * as members from "./members.js";
import * as online from "./online.js";        // ← 加这行

export const commands = [members, online];    // ← 加进数组
```

**③ 重新部署 + 重新注册**

```bash
npm run deploy      # 上传新代码
npm run register    # 把新命令登记到 Discord（guild 级秒生效）
```

主入口 `index.js`、`register.js` **都不用动**。

---

## 6. 部署全流程（首次）

> 分工：🌐 = 浏览器里你本人操作（登录你的账号、拿只有你能看的密钥）；💻 = 终端命令。

### 6.1 🌐 创建 Discord 应用

1. https://discord.com/developers/applications → **New Application**（每个服务一个独立应用）。
2. **Bot** 页 → Reset Token，保存 **Bot Token**（机密）。
3. ⚠️ **Bot 页 → Privileged Gateway Intents → 打开 `SERVER MEMBERS INTENT`**
   （凡是要读成员名单的服务，不开这个 REST 就拉不到人）。
4. **General Information** 页记下 **Application ID** 和 **Public Key**（都不是机密）。
5. **OAuth2 → URL Generator**：勾 scope `bot` + `applications.commands`，
   用生成的链接把机器人**邀请进服务器**（不邀请，注册命令会 403）。

### 6.2 💻 部署 Worker

```bash
cd server-bot
npm install

npx wrangler login                          # 🌐 浏览器授权 Cloudflare 一次（一次性）

# 设两把密钥（顺序：先设密钥，deploy 上去才不会一跑就缺环境变量）
printf '%s' '<Public Key>' | npx wrangler secret put DISCORD_PUBLIC_KEY
npx wrangler secret put DISCORD_TOKEN        # 交互式，粘贴 Bot Token（机密，自己贴）

npx wrangler deploy                          # 得到 https://server-bot.<子域>.workers.dev
curl -s https://server-bot.<子域>.workers.dev   # 应返回 "server-bot up"
```

> **`wrangler login` vs `deploy` 的区别**：
> `login` = 授权「你这台电脑的 wrangler」能管你的 Cloudflare 账号（一次性门禁，与 Discord 无关）。
> `deploy` = 把代码打包上传到 Cloudflare 边缘、生成那个 24 小时在线的公网网址。

### 6.3 🌐 回填 Interactions Endpoint URL

Developer Portal → 你的应用 → **General Information → Interactions Endpoint URL**
填入 Worker 网址 → **Save**。Discord 会即时发 PING 校验，验签通过才保存得上
（所以必须**先 deploy 成功再填**）。

### 6.4 💻 注册斜杠命令

在 `server-bot/` 建 `.env`（照 `.env.example` 填 App ID / Token / Guild ID），然后：

```bash
node --env-file=.env register.js             # 看到 ✅ /成员列表 即成功
```

### 6.5 验收

回 Discord 打 `/成员列表`，看到成员名单即全线跑通。

---

## 7. 日常运维

| 需求 | 命令 |
|---|---|
| 改了代码，重新上线 | `npm run deploy` |
| 加/改/删了命令，重新登记 | `npm run register` |
| 看实时日志（调试线上问题） | `npx wrangler tail`（再去 Discord 触发命令即可看到请求） |
| 改某个密钥 | `npx wrangler secret put <名字>`，即时生效，无需重新 deploy |
| 查密钥列表 | `npx wrangler secret list` |
| 查登录账号 | `npx wrangler whoami` |
| 本地起开发服 | `npm run dev` |

---

## 8. 排错手册（含本次踩过的坑）

| 症状 | 原因 | 解决 |
|---|---|---|
| 命令一直停在「思考中…」 | 回了延迟应答，但后台回填那步失败且被静默吞掉 | `wrangler tail` 看日志；`editOriginal` 已改成失败时打印状态码+返回体 |
| 「拉取成员失败」 | 没开 `SERVER MEMBERS INTENT`，或 `DISCORD_TOKEN` 没设对/已失效 | 去 Bot 页打开意图；重设 token 密钥 |
| 保存 Endpoint URL 报错 | 还没 deploy，或 `DISCORD_PUBLIC_KEY` 设成了 Token | 先 deploy 成功；确认设的是 Public Key |
| 打 `/` 看不到命令 | `register.js` 没跑成功 / GUILD_ID 错 / 机器人没被邀请进服务器 | 重跑 register；核对 guild；用 OAuth 链接邀请 |
| 注册命令报 403 Missing Access | 机器人没在该服务器里 | 先用 `bot`+`applications.commands` 链接邀请 |
| `curl` 刚部署完返回 `error code: 1042` | 边缘节点还在传播的瞬时错误 | 等几秒重试即可 |
| 换了新 token 后命令失效 | Worker 密钥里还是旧 token | `wrangler secret put DISCORD_TOKEN` 重设；`.env` 也同步换 |

---

## 9. 安全约定

- **Token 是机密**（= 机器人的完整控制权）。不要贴进聊天、不要提交进仓库。
  一旦外泄，去 Developer Portal → Bot → **Reset Token**，再用 `wrangler secret put` 换上新值。
- **Public Key / Application ID / Guild ID 不是机密**，可放心写进配置。
- `.env`、`.dev.vars` 已在 `.gitignore` 里，不会入库。
- Worker 端的密钥用 `wrangler secret` 存（加密），**绝不写进 `wrangler.toml`**。
- 权限收口：像 `/成员列表` 这种管理向命令，用 `definition.default_member_permissions`
  限制到「管理服务器」等权限（本命令设的是 `"32"` = MANAGE_GUILD）。
```
