# 潜客流水线 · 最终名单契约（Prospect Pipeline Contract）

> 定义「潜客生成 → HubSpot 富化 → 排序 → 交付」整条链流动的**富记录**结构与排序规则。
> matcher、潜客生成、情报台三处都照此契约对接。版本 v0.1。

---

## 1. 核心原则：matcher 退为「富化步骤」，不再是最终格式

老 matcher 把一切压成 6 列（company/domain/status/owner/计数），丢掉了意向分、信号标记、排名。新设计反过来：

```
潜客生成（富记录，带信号意向）
        ↓
HubSpot 富化（matcher：只往每条记录贴 HubSpot 状态，不新建表）
        ↓
综合排序（意向 × CRM 系数）
        ↓
富列表交付（xlsx + 情报台「今日名单」+ 早报数据源）
```

matcher 的爬虫内核 `HubSpotBrowser.search` 原样复用；只改输入输出——**输入富记录、输出并回富记录**。

---

## 2. 富记录 schema（最终每行带的字段）

| 字段 | 来源 | 说明 |
|---|---|---|
| `rank` | 综合计算 | 最终排序位次（按 weight 降序） |
| `weight` | 综合计算 | `intent_score × crm_multiplier`，见 §4 |
| `intent_tier` | 信号库 | A / B / C |
| `intent_score` | 信号库 `query_for_node` | 该公司所在赛道/元件的余料信号聚合强度 |
| `surplus_signals` | 信号库 | 命中的信号标记（如「EOL潮; 车规寒冬」）+ 可溯源 signal_ids |
| `track` | 生成 | `coverage`（覆盖轨，占 100 配额）/ `opportunity`（机会轨 bonus，不占配额） |
| `hubspot_status` | matcher | `matched` / `no_match` / `multiple_exact_matches` / `matched_owner_empty` |
| `hubspot_owner` | matcher | 认领的销售（如有） |
| `crm_state` | matcher 派生 | `new` 新客 / `unowned` 在库未认领 / `owned` 已认领 / `review` 多重待核 |
| `company_name` `website` `country` | 生成 | 基础信息（website→matcher 的 domain） |
| `contact_rationale` | 生成 | 为什么是潜客 + 用哪类元件 |
| `components` | 生成 | 元件大类 |
| `seen_before` | 本地历史库 | 往期是否出现过（标记不删） |
| `sources` | 信号库 | 命中信号的来源 URL |

---

## 3. CRM 状态映射（matcher 结果 → crm_state）

| matcher 返回 | crm_state | 含义 |
|---|---|---|
| `no_match` | `new` | HubSpot 里没有 → 全新潜客 |
| `matched` + owner 空 | `unowned` | 在 CRM 但没人认领 |
| `matched` + 有 owner | `owned` | 已被销售认领 |
| `multiple_exact_matches` | `review` | 多重命中，待人工核 |

---

## 4. 排序：意向 × CRM 系数

```
weight = intent_score × crm_multiplier
```

| crm_state | crm_multiplier | 效果 |
|---|---|---|
| new        | 1.0 | 高意向新客浮顶 |
| unowned    | 0.7 | 在库但没人碰，值得跟 |
| review     | 0.6 | 多重命中，待核 |
| owned      | 0.3 | **保留但压底**，并标「已认领」 |

> **已认领公司不删除**：留在名单里、排到最底部、打 `crm_state=owned` 标记，你自己判断要不要再碰。
> 于是「高意向 + HubSpot 里还没人碰」的公司自动排最前——那是最该打的。

`intent_score` 取该公司 sector/component 命中信号的衰减强度之和（同 `detect_hot_sectors` 的聚合口径）；映射 tier：A ≥ 8、B ≥ 3、C < 3（阈值可调）。无命中信号 → intent_score 给一个基线（如 1.0），仍可被 CRM 系数区分。

---

## 5. 两条轨道在最终名单里的呈现

- **覆盖轨**（coverage）：当天潜客树节点生成的 ~100 家，富化 + 排序后是名单主体。
- **机会轨**（opportunity）：信号点名的具体公司，`track=opportunity`、**不占 100 配额**，同样富化 + 匹配，作为附录/置顶高亮（视 weight）。

已认领的压底逻辑对两轨一致。

---

## 6. 交付形态

1. **富 xlsx**：列即 §2 字段，按 rank 排好；已认领段落置底。`surplus_signals` 列写人话标记 + 角标可点开来源。
2. **情报台「今日名单」**：同一份数据在前端表格化，支持按 tier/crm_state 筛选、点开信号溯源、一键下载 xlsx。
3. **早报推送**：取名单 top N + 机会轨 bonus 作为「今日要点」的线索部分。

---

## 7. 现有 matcher 要改什么（worker.py / service.py）

- **保留**：`HubSpotBrowser`（爬虫内核）、`exact_match` / `process_one`（匹配算法）不动。
- **改 `process_file` 的 I/O**：
  - 输入从「读 Excel(company,domain)」改为「接收富记录列表」。
  - 每条调用 `process_one` 得到 status/owner 后，**派生 crm_state、算 weight、并回原记录**（不丢字段）。
  - 输出从「写 6 列 Excel」改为「写富 xlsx + 返回富记录列表」，按 weight 排序、已认领置底。
- **去掉**：Web 任务表/队列那套（service.py 的 TaskManager）——集成进 jarvis 后用 jarvis 的调度，不需要。

---

## 8. 待落地顺序（建议）

1. matcher 富化改造（worker.py 的 process_file I/O + crm_state/weight）。
2. 潜客生成 Step 3 重写（产出富记录、接 `signal_for_node` + 机会轨）。
3. 情报台前端（模态浮层 + 早报推送）读这份富数据。
4. 每日采集 + 全流程挂 jarvis 调度。
