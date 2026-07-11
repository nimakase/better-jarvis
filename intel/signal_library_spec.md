# 信号库规格（Signal Library Spec）

> 共享情报层的**公共契约**。一处采集、多处消费：同一份信号同时喂「每日市场情报日报」和「潜客生成」。
> 版本 v0.1 — 设计稿，落地前的唯一事实来源。

---

## 1. 核心原则：采集 ≠ 消费

信号到达是随机的，潜客树推进是确定的——**两者不需要在同一天相遇**。信号库就是它们之间的「记忆/缓冲」。

- **采集**：每天**全行业广扫**，扫到什么就**按维度打标存库**，不管今天潜客树轮到哪个节点。
- **消费**：各出口只是**查询**同一个库，互不干扰。
  - 日报 → 查「最近 N 天全行业」切片 → 广度。
  - 潜客 → 按当天节点的赛道/元件查相关信号（不论新旧）→ 深度。

> 广采集是日报好用的前提，而广采集恰好让潜客侧永远有库存信号可查。两个需求解耦后互相成全。

---

## 2. 存储

- 本地 **SQLite**：`signal_library.db`（查询零成本、瞬时，符合"本地去重"思路）。
- 位置：随系统集成进 jarvis 后移到 jarvis 数据目录；当前阶段先置于 Autoworker 下。

---

## 3. 表结构（DDL）

```sql
-- 主表：一条 = 一个信号
CREATE TABLE signals (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint     TEXT UNIQUE,          -- hash(source_url + 归一化标题)，用于去重/合并
  date_collected  TEXT NOT NULL,        -- 采集日 YYYY-MM-DD
  date_event      TEXT,                 -- 事件实际发生日（已知则填）
  source_url      TEXT,
  source_type     TEXT,                 -- news | press_release | earnings | distributor | forum | other
  scope           TEXT NOT NULL,        -- macro | sector | component | company
  signal_type     TEXT NOT NULL,        -- 见 §4
  direction       TEXT,                 -- up | down | flat | na  （价格/交期/需求用）
  severity        INTEGER,              -- 1-5 重要度/热度
  summary         TEXT NOT NULL,        -- 一句人话（日报渲染用）
  surplus_implication INTEGER DEFAULT 0,-- 0-5：多大程度意味"有余料生成"。0=纯行情、只进日报
  confidence      INTEGER,              -- 1-5
  half_life_days  INTEGER,              -- 时效（见 §6）
  status          TEXT DEFAULT 'active',-- active | expired | merged | reviewed
  raw_json        TEXT                  -- 完整抽取原文/结构化 blob
);

-- 多对多标签：sector_id 对齐 prospect_tree 的赛道/叶子 id（潜客查询的钩子）
CREATE TABLE signal_sectors    (signal_id INTEGER, sector_id TEXT);
CREATE TABLE signal_components (signal_id INTEGER, component TEXT);  -- MCU/FPGA/MOSFET/IGBT/MLCC/存储...
CREATE TABLE signal_regions    (signal_id INTEGER, region TEXT);

-- 信号点名的具体公司 → 机会轨「点名公司」来源（金线索）
CREATE TABLE signal_companies (
  signal_id INTEGER, company_name TEXT, website TEXT, country TEXT, note TEXT
);

-- 人工审核队列：行业级强信号 → 你确认后才提前做该赛道
CREATE TABLE review_queue (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  sector_id   TEXT,                 -- 命中的赛道
  reason      TEXT,                 -- 为何上榜（聚合了哪些信号）
  signal_ids  TEXT,                 -- 关联信号 id 列表（JSON）
  status      TEXT DEFAULT 'pending', -- pending | approved | rejected
  created_at  TEXT, decided_at TEXT
);
```

> 简化备选：标签表也可塞成 signals 里的 JSON 列；这里用规范化表是因为「按 sector 查」是潜客最高频操作，join 最省事。

---

## 4. signal_type 分类（一套分类两边用）

`signal_type` 本身就是**日报的章节**；其中带★的同时是**潜客的意向依据**（surplus_implication 通常 ≥ 1）。

| signal_type | 含义 | 日报 | 潜客 |
|---|---|---|---|
| pricing | 涨/跌价 | ✓ | |
| lead_time | 交期变化 | ✓ | |
| shortage | 缺货/紧张 | ✓ | |
| oversupply ★ | 过剩/跌价踩踏 | ✓ | ✓ |
| eol_pcn ★ | 停产/产品变更通知 | ✓ | ✓✓ |
| demand_shift ★ | 终端需求转向/下滑 | ✓ | ✓ |
| capacity | 扩产/减产 | ✓ | ✓ |
| layoff ★ | 裁员 | ✓ | ✓ |
| closure ★ | 关厂/退出业务 | ✓ | ✓✓ |
| m_and_a ★ | 并购/重组 | ✓ | ✓ |
| write_down ★ | 库存减值（财报） | ✓ | ✓✓ |
| policy | 关税/出口管制/补贴 | ✓ | |

> 日报看左半（direction + summary），潜客看右半（surplus_implication + sectors/components 标签）。同一条信号两边各取所需。

---

## 5. 消费路径

### 5.1 日报视图（广度）
查最近 N 天 `status='active'` 的信号，按 `signal_type` 分组渲染成 PDF 各章节。**对全行业动向真实有用**——这是广采集的直接回报。

### 5.2 潜客 · 覆盖轨（深度，占 100 配额）
- 输入：当天潜客树按既定顺序选出的节点（**顺序绝不被信号改写**）。
- 取该节点的 `sector_id` + 相关 `components` → 查库里 `surplus_implication ≥ 1` 且**衰减后强度 > 阈值**的信号。
- 这些信号成为该节点公司的**意向加分**和 `Surplus Signal` 列的"为什么现在"依据；查不到就退回画像 + 宏观。

### 5.3 潜客 · 机会轨（锦上添花，**不占** 100 配额）
- **点名公司**：`signal_companies` 里 `surplus_implication ≥ 3` 的 → 经历史库去重（重复则标记不删）后，作为 **bonus 线索附录**直接附在当天名单后。通常很短，纯属白捡。
- **点名行业**：某 `sector_id` 聚合出高强度信号但不是当天节点 → 写入 `review_queue(status=pending)` → **你人工确认** → 批准则**提前生成该赛道并标记 done**（于是它不会再被正常轮到，其余树顺序丝毫不变）；否决则搁置。

---

## 6. 时效与去重

- **衰减**：`有效强度 = severity × decay(age, half_life_days)`。按类型给默认半衰期：
  - 价格/交期 ~21 天；缺货/过剩/需求转向 ~45 天；裁员/扩减产 ~90 天；EOL/关厂/并购/减值 ~120 天；政策 ~180 天。
  - 超期自动 `status='expired'`（日报不再显示，潜客不再加分）。
- **去重/合并**：同一事件反复出现时按 `fingerprint` **合并并刷新**（提升 confidence、更新 date_collected），不产生重复行。
- 与潜客**历史库**的关系：信号库管"行业在发生什么"，历史库管"哪些公司已产出过"，两者独立。

---

## 7. 待落地（本规格定稿后的下一步）

1. 建库 + DDL 初始化脚本。
2. **每日全行业信号采集**（大模型联网 → 结构化抽取 → 入库去重）——这是要写的核心提示词之一。
3. 把现有 `market_intel_report` 从**写死的假数据**改成**读信号库**渲染（前置依赖：信号库得先有真数据）。
4. 潜客生成 Step 3 接入 5.2 / 5.3 的查询。
