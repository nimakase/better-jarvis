# 投递与容错契约（Delivery & Resilience）

> 定义三轨解耦、matcher 容错、投递控制（对话 + 休假 + 记忆联动）、Web Push。
> ③情报台 与 ④调度 照此建。版本 v0.1。

---

## 1. 三轨解耦

| 轨道 | 依赖 | 产出 | 失败影响 |
|---|---|---|---|
| 信号采集 | 联网 | 信号库 | 失败 → 当天无新信号，两轨用存量信号照跑 |
| **日报轨** | 仅信号库 | 日报 → Web Push | **完全不碰 matcher** |
| **潜客轨** | 信号库 + 生成 + matcher | 富名单 → Web Push | matcher 失败**只困本轨**（降级），不波及日报 |

两轨各推各的，互不阻塞。

---

## 2. matcher 容错（HubSpot API 不可用，保留浏览器爬取）

1. **预检**：跑批前用爬虫内核 `check_session` 静默验登录。无效 → 早通知（Web Push），不空跑。
2. **降级**：登录挂了仍出清单——`crm_state=pending`、名单顶部横幅「本批未做 HubSpot 匹配，恢复后可一键补匹配」。生成+意向排名不依赖 HubSpot。
3. **单公司隔离**：单条匹配异常 → 该行标 `status=error/crm_state=unknown`，整批继续（已在 `pipeline.enrich_records`）。
4. **补匹配**：登录恢复后，对 `pending` 行重跑 matcher 富化，刷新排名。

### 报错分级 → 通知路由

| 级别 | 例 | 处理 |
|---|---|---|
| 瞬时 | 网络抖动、页面超时、浏览器崩溃 | 自动重试/重建，仅记日志，不打扰 |
| 单行 | 某公司匹配失败 | 汇总成「匹配失败 N 家」角标 |
| **高** | 登录过期、HubSpot 改版、列/配置错 | **Web Push** + 情报台红色错误卡 + 落库 error_message |

---

## 3. 投递控制（你要的"和 jarvis 沟通停推"）

**状态**：每条轨道独立维护 `{status: active | paused, paused_until, reason}`，本地存（DATA_DIR）。

**对话控制**：你直接对 jarvis 说——
- 「日报停 3 天」「潜客清单这周别推」→ 改对应轨道状态。
- 「恢复日报」→ 置回 active。
- 「现在投递什么状态」→ 回报两轨状态。

每条调度任务**推送前先过闸门**：paused 则跳过（不产出/不推送）。

**Agent 工具**（connector）：`delivery_pause(track, until)` / `delivery_resume(track)` / `delivery_status()` / `set_rest_period(start, end)`。

---

## 4. 休假联动 + 记忆（更自主）

- **默认**：你标记的休假期间，**两轨都不推送**（freeze）。
- **记忆联动**：休假区间、推送偏好写入 jarvis 记忆（`core/memory` + `memory_tools`）。jarvis 据此**更主动**：
  - 休假临近时主动确认「这几天日报/潜客都暂停，对吗？」
  - 逐渐学到你的习惯（如常在周末暂停），下次主动提议而非每次问。
- **潜客树冻结**：潜客轨暂停期间**不推进行业树**——节点留着，你回来接着做，不烧赛道。
  （日报轨暂停不影响信号采集，信号库照常积累；恢复后日报覆盖近窗即可。）

---

## 5. Web Push 落地

jarvis 前端已是 PWA（`frontend/sw.js` + `manifest.webmanifest`）。补：
- 生成 **VAPID** 密钥对（存 DATA_DIR / 配置）。
- 前端：请求通知权限 + 订阅，订阅信息回存后端。
- 后端：`web/push.py` 推送端点 + `pywebpush` 发送。
- `sw.js`：加 `push` 事件处理，展示通知、点击跳情报台对应卡。
- 用途：日报就绪 / 潜客清单就绪 / 高级别报错 / 休假确认。

---

## 6. 待落地（归入 ③④）

1. 投递控制状态 + connector 工具 + 调度闸门。
2. matcher 预检 / 降级 / 补匹配。
3. Web Push 全链（VAPID + 订阅 + 端点 + sw.js）。
4. 休假/偏好写入 jarvis 记忆 + 主动确认话术。
5. 情报台展示两轨状态、错误卡、补匹配按钮。
