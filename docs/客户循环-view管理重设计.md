# 客户循环重设计:priority 字段 → 贾维斯管理的私有 view

> 状态:设计定稿待确认一处(见「待确认」①)。2026-07-29。未动代码。

## 1. 背景与转向

HubSpot 公司层面**下线 priority(hot/warm/cold)字段**。Ned 仍需这套分类做冷开发与维护管理。

结论:**分类逻辑保留,值不再写 HubSpot 字段**;改由贾维斯把账户分段**落成私有账户 view**——Ned 照旧在 HubSpot 里打开 view 干活。副作用:不再往 HubSpot 写 priority,`cold/dead reset 工作流`的风险自然消除。

## 2. 可行性(已实测,2026-07-29)

- **名单法落 view** ✅:账户过滤器 `Account name` + `contains exactly`,值旁铅笔 → "Edit values" 文本框(每行一个名)→ Set values。贾维斯可驱动,定义任意 view 成员。
- **账户层过滤器看不到 Sequence 状态**(只有 "Sequence Opt-Out")✅:所以轮次分段必须靠贾维斯汇总,原生过滤器做不到。
- **读 outreach 历史 = Breeze** ✅:在 HubSpot Copilot(右上 ✦ Assistant)问"列出该账户 Ned 发出的 outreach 邮件,返回 JSON",Breeze **用真 CRM 工具**查、返回**干净可解析 JSON**(date / subject / to_contact / **job_title 岗位**,有则带上),非编造。样例 Insta Elektro:Annette 收 4/8+4/10+4/14 三封;没成 → 换 Sascha/Alfred/Christof 各三封。

## 3. 数据流

1. **触发**:冷启动全量;之后增量(只重查 last activity / last engagement 变过的账户)。
2. **读**:HubSpot 读 type(有无 deal)、活动/往来日期、**账户关联的全部联系人名单**;Breeze 抽 outreach 邮件历史(逐封:日期 / 标题 / 联系人 / **岗位**)。
3. **算(贾维斯,确定性)**:**先按联系人聚合**(每联系人几封 / 是否回复 / 岗位)→ 再推账户状态(到顶 / 回复 / 还剩几个没试过的联系人);core 算维护是否到点。
4. **落**:把每个分段的账户名单写进对应私有 view(名单法);回信约了时间的建 Task。
5. **交付**:夜报 + Ned 打开 view 干活。

**分工原则**:Breeze 只抽事实(邮件日期/标题/联系人),**不下结论**;轮次/上限/维护间隔等确定性判断贾维斯算。返回可核对的事实而非"第 N 轮"。

## 4. 状态模型 & view 清单

**判定极简(2026-07-31 定):不数联系人/轮次**,只看两件事——有没有人回信、最后一封 outreach 离现在多久。联系人明细(谁发过几封、岗位、还剩谁没试过)照样从 Breeze 抽出来放进账户详情,供 Ned 换人时参考,但**不参与状态判定**。

**Prospecting(无 deal)—— 冷开发流:**

| view | 判定 | 动作 |
|---|---|---|
| 未开发 | 无 outreach 记录 | 去启动第一轮 |
| 开发中 | 最后一封 outreach 在 14 天(ACTIVE_DAYS)内 | sequence 还在跑,不用管 |
| 待处理·换人或放弃 | 有发过、最后一封 >14 天、且没人回 | **归一类**:Ned 定换人发 or 放弃 |
| 已回复·待跟进 | 有联系人回信(优先级最高) | sequence 自动停;约了时间就建 Task |

**Core(有 deal)—— 维护流,绝不冷开发:**

| view | 含义 | 动作 |
|---|---|---|
| 维护到点 | 距上次 touch > 2 个月 | 提醒 Ned 联系 |
| Core·冷处理 | 低质(线材塑料头那类),手动钉休眠 | 搁置,有机会再看 |

## 5. 参数(已定)

- **ACTIVE_DAYS = 14**:最后一封 outreach 在 14 天内 = 开发中;超了 = 待处理。
- Core 维护间隔 = **2 个月(60 天)**未 touch。
- 换联系人 = **不自动**,Ned 判断(贾维斯只在账户详情摊出已试/未试联系人+岗位供参考)。

## 6. 待确认 / 风险

1. **名单法精度**:实盘用 `is equal to any of`(整名精确,已建议)比 `contains exactly`(子串,会误伤如 "Insta Elektro" 命中 "Insta Elektro GmbH")稳。值数量上限(几百 prospecting 时)待验。
2. **贾维斯批量驱动 Breeze**:逐账户开 Copilot 问 + 解析 JSON;增量下量可控,冷启动慢、可分批。选择器 + 坑已实盘校准(见 breeze_outreach 顶部注释)。
3. **store schema**:per-account `{state, view, last_outreach, days_since_last, tried_contacts[{name,job_title,n_sent,last,replied}], untouched_contacts, replied_contacts}`(见 outreach_state 返回)。

## 7. 分阶段落地建议

1. store schema + Breeze 抽取封装(开 Copilot → 问 → 解析 JSON)。
2. 轮次 / 维护判定逻辑(纯函数,可单测)。
3. 名单法写 view(驱动封装;先拿单个 view 验 Set values + Save 持久化)。
4. 编排:夜跑 = 读 → 算 → 更新各 view + 建 Task + 夜报。
5. 冷启动分批灌;之后走增量。

## 附:保留不变的部分

- `account_grading.classify()` 的 type 判定(有 deal → core,单向不降级)不变。
- 读取完整性闸、池成员变动检测保留。
- cold/dead 硬护栏(`BLOCK_COLD_DEAD_WRITE`)在 priority 不再写 HubSpot 后成为死代码,可随迁移一并清理。

---

## 附二:v2 大重构讨论纪要(2026-08-05,进行中,待收尾画整体框架)

背景:公司发布《Time-Decay Account Cap》AM 操作手册(v1.0, 2026-05,见上传 PDF)。它把 Core/Prospecting 变成有硬语义的政策开关,直接约束本框架。以下为与 Ned 逐条敲定的结论。

### 已定

1. **两层模型(核心 reframe,别揉一起)**:
   - **① HubSpot Account Type = Core / Prospecting** —— 公司政策开关,**只决定是否被 time-decay 自动回收**;Ned 手动设。Core 上限 450(保护、永不 decay),Prospecting 上限 550(6 个月无 qualifying activity → owner 清空、回收,不可逆)。
   - **② 贾维斯分层 = 价值判断**(T0 / 低core / 潜在 / 冷 / dead)—— 落私有 view,管优先级与动作。
   - Type 管保护,分层管动作。记忆里"写 cold/dead 被工作流夺账户"的坑 = 就是这个 time-decay 回收机制。
2. **Core 准入 gate**(不能"发过 list 的一律 Core",会撑爆 450 且稀释语义):Won / 进行中 deal / Lost 但料好可期 → Core;**Lost 且料差(线材插头类)→ 不进 Core**,丢 Prospecting 或手动休眠。
3. **Core 内分层**:多个 Won = T0 长期伙伴(维护勤);1–2 Won = T1 低频出货(维护疏);Lost 料好 = T2 潜在未来(定期回访);进行中 = 活跃单(推进)。维护间隔是**贾维斯自定 nudge**(core 本身不 decay),非政策。
4. **Prospecting 的 decay 是特性不是 bug**:放弃一个冷客户 = **什么都不做,让它自己 decay 掉**,不用手动标 dead / 释放。
5. **硬护栏**:贾维斯**只读不写 Account Type、绝不动 owner**(写错→ Core 变 Prospecting 被回收 / 撑爆 450)。且**不得为凑活动乱记 note/task**——政策明确贾维斯的 note/task 也算 qualifying activity 会重置 decay 时钟,会把本想 decay 掉的账户救活。
6. **sequence = Ned 自己写自己发**:3 轮 × 每轮 3 封同标题连发,轮间隔 **2 个月**(已确认是其工作习惯)。每封都 reset decay 时钟,故跑三轮期间账户不会 decay;三轮跑完停手,decay 才开始倒数。
7. **不 opt-out 公司 auto-sequence**(Ned 决定):停手约 4.5 个月后系统用**其身份**自动补一轮 = 白送一次尝试;钓到回复则账户凭"收到回复"自动保住,钓不到照样 decay,不亏。
8. **坑**:公司 auto-sequence 用 Ned 身份发,Breeze 抽历史会混入 → **必须从轮次计数里剔除**。识别:①与第三轮之间必有 >4 个月大空档;②Marketing 模板≠ Ned 自拟标题。
9. **Prospecting 状态机 = 两个时钟并行**,替换旧 `ACTIVE_DAYS=14`:
   - **节奏钟**(2 月一档,轮次驱动非天数):没发过→发第一轮;三轮没完→别动,距上封 ≥2 月才提醒"发下一轮";三轮完+没回→决策点(换 contact 重开 / 撒手)。
   - **死线钟**(读 HubSpot 原生 **Decay Stage** 属性):撒手后进 decay,临近 final warning 单独拎出提醒 Ned 最后决定;decay 掉则从池成员检测归档 store 记录。
   - 回复随时可能从任一轮(含公司轮)冒出,一旦有跳"回复四分类",压过一切。
10. 轮次数可从 Breeze(同标题簇 / 日期)自动反推,不用手输。
11. **分工**:贾维斯 = 观察(读 Type/deal/活动/回复/Decay Stage)+ 算(分层 + decay 风险)+ 摆台面(维护 view、拟 task、夜报);Ned = 手动写(设 Type、发件、建/输 deal、释放、亲自回)。Ned 每个手动动作 = 贾维斯下一轮重读的输入,不覆盖。

12. **交互层 = 飞书多维表格(Bitable)当常驻驾驶舱 + 交互卡片当一次性推送**(2026-08-05 定)。Bitable 是飞书内独立常驻应用:不被聊天流顶走、飞书托管(无需公网暴露/Cloudflare/迁云)、走现有 app-token 鉴权、服务端 API 全 CRUD(批量写 ≤500/次,读回 Ned 的行编辑)。各段 view = 同一张表的筛选视图(未开发/开发中/待处理/已回复/维护到点/**待你决定**)。Ned 在表里干活 = 秒开 API 读;他改某行状态字段 = Q2 里那个便宜的"已处理"处置信号,**全 API、不碰浏览器**。交互卡片(`lark_bridge` 已有 `_send_card`/`_on_card_action`/`_update_card`)只做热信号"叮"一下,被划走无所谓。
13. **交互层边界**:慢的只剩 **Bitable ⇄ HubSpot 后台同步**(仍浏览器爬/写,压后台批量,不糊 Ned 脸)。人面对的 Ned ⇄ Bitable ⇄ 贾维斯 全快 API。"贾维斯只读不写 HubSpot Type/owner"硬护栏不变 —— Bitable = 贾维斯管理层 + HubSpot 状态镜像,**不是把 HubSpot 变可远程遥控**;要落 HubSpot 的改动仍 Ned 手动、或贾维斯只碰安全项。落地成本:卡片现成;Bitable 净增(加 API scope + 小客户端,鉴权复用)。

14. **回复分类 = 复用 `reply_router` 的业务 6 类**(2026-08-05 按"类别 = 动作,动作相似不拆"归并锁定;原 8 类里 no_stock/nda/has_channel 三者动作相同 → 合并):`list_relevant`(发来可做的料 → 你建 deal 转 Core;build_deal + list_quality=good)、`list_irrelevant`(成品/不可做 → list_quality=junk,喂 not-core/L6)、`interested_later`(有兴趣当下无货:暂无货/卡 NDA/已有渠道**三合一** → Note + 带唤醒日 Task,唤醒天数由分类器 `stock_wake_days` 带入,原因&对手写进 Note 摘要)、`explicit_no`(明确不做 → 停 seq opt-out + 释放)、`referral`(转介 → switch_contact 换人)、`pleasantry`(客套 → 无动作存档)。**不加 `unclear`**(收益小,Ned 定)。动作从**下线的 priority 重定向到 v2 字段**(list_quality/build_deal/switch_contact/sequence_opt_out/task/your_action)。none/解析不出 → 无动作。复用 `reply_classify` + `reply_router`,单测已改绿。
15. **list 质量数据源 = Ned 在 Bitable 一列写自然语言 note,LLM 读进好/弱/差(并保留"为什么")**。**不**写成 HubSpot note(慢读 + 记 note 会重置 decay 时钟、把想放弃的账户救活)。贾维斯预填猜测(有无 deal / loss reason / deal 备注)让 Ned 少动手。全自动判(喂 CCL 能力清单 + Breeze 抽 line-item + LLM 判)= future upgrade。
16. **"放弃 = 无声淡出,不是当面切断"**(化解"观感 vs 容量"纠结):放弃 = 停止主动发 + 让 time-decay 悄悄退回 No owner,客户收不到任何信号。真正丢分的是反面 —— 给已知死料客户自动 sequence。故:①明确永远做不了(结构性不相干)→ 不占 Core、不主动 seq、静默 decay,并**按单账户 opt-out** 公司 auto-sequence(手册 p10 允许对特定账户退出),连那轮没意义的自动发信都省;②这次垃圾料、但公司本身做电子、以后可能有货 → 留 Prospecting 轻触。decay 掉只回 No owner、可 reclaim,桥没烧。
17. **L5(Lost + 料弱但有潜力)= 默认丢 Prospecting 开发、可升 Core**(不占稀缺 Core 保护位;发得动转正、发不动自然 decay)。同第 16 条②。

18. **Core 维护间隔(已定)= T0 45 天 / T1 90 天 / T2 60 天**。触发 = 距上次 activity(复用 HubSpot **Last Activity Date**)超阈值且期间无任何 active → 提醒 Ned 联系;任何 touch 重置。Core 永不 decay,此为纯关系保温 nudge,与公司 decay 死线钟无关。

### 收尾

- 整体框架图已画(三方闭环:HubSpot 真相源 / 贾维斯 / 你,经多维表格协作;含分类引擎、decay 双时钟、指纹回路、慢只在 HubSpot⇄贾维斯 一处)。设计讨论到此收束,**18 条已定 + 框架图**。下一步转实现规划(store schema 扩展、状态机重写、Bitable 客户端、编排接线)。

---

## 附三:实现规划要点(2026-08-05,待统一开工)

### A. 数据获取(贾维斯读 HubSpot)—— 2026-08-05 核过 HubSpot 实况后定稿
- 机制:`account_reader.py` 爬某 HubSpot 列表视图的表格(列名匹配 + 虚拟滚动 + 渲染完整性收行)= "把需要的列摆到视图上,贾维斯扫表"。
- **三个新信号,HubSpot 实况 + 兜底方案**(Ned 核过:won/decay 都没有,domain 有):
  - **Won deal 数** —— HubSpot **无此列**(无原生赢单数汇总)→ 改**走 Breeze**:`breeze_outreach.build_deal_prompt` / `ask_deal_summary` 问 CRM 数 won/lost/open,won 喂 `core_tier`。仅对 Core 账户查、可增量。
  - **Decay Stage** —— HubSpot **无此列** → 改**推导**:`outreach_state.derive_decay_stage(last_activity)` 用 Last Activity Date 近似公司 6 个月线(None/in_decay/final_warning/reassigned,估算)。
  - **Company Domain Name** —— **有**,Ned 加成列 → `account_reader` 读 `company_domain` → 作 `website=` 传给 `ask_breeze`/`ask_deal_summary` 消歧。
  - `account_reader` 里 `num_won_deals`/`decay_stage` 列候选保留但通常匹配不到(无害,将来 HubSpot 若开了列自动启用);撤 **Priority** 列(随 priority 下线)。

### B. Breeze 抽取:prompt + 重名消歧(堵 401 成因之一)
- 病根:重名时 Breeze 弹"你指 A 还是 B?"→ 无 JSON/完成标记 → 60s 超时 → 空 → 误判 `not_started`(= 401 mislabel 的一个成因)。堵它一箭双雕。
- **prompt 层**(`build_prompt`,**必须单行**):最前加"直接指定"句 —— A 即精确目标、**禁止反问/消歧/列他司**;多个近似名只用**完全等于 "A"** 的那个;无精确匹配则 `found:false`;绝不以问题作答。有 domain 则注入 `named "A" (website: a.com)`(需 `account_reader` 多读公司网址列)。
- **脚本层安全网**(`_send_and_poll`):轮询中识别复核提问(含 `did you mean / which company / multiple companies` 或带问号求确认且无 JSON)→ 自动回 `Use exactly "A". Do not ask again.` 一次;仍问 → **带明确错误状态退出(非空)**,绝不静默返回空再被当 `not_started`。

### C. 两块屏并存(同一份 store 喂)
- **Bitable = 决策驾驶舱**(读状态、定夺、note、处置勾选)。
- **HubSpot 视图 = 动手面**(enroll sequence / 批量操作只能在 HubSpot 干)。写法 = **名单法** `view_writer.set_view_membership`(铅笔 → Edit values → 填名单 → Set values),已实盘验证。Ned 预建好视图,贾维斯往对应段写名单。

### D. 开工顺序(承附一 §7 + 本次)
1. store schema 扩展:轮次 / 分层(T0/T1/T2)/ Decay Stage / list 质量 note / 处置态。
2. `account_reader` 加列(Decay Stage、Won 数、可选 domain)。
3. Prospecting 状态机重写:替 `ACTIVE_DAYS=14` → 轮次 + 2 月催发 + 读 Decay Stage;剔除公司 auto-sequence 邮件(大空档 + 非本人标题)。
4. `breeze_outreach`:prompt 消歧 + 安全网。
5. 回复六分类接 `reply_classify`。
6. Bitable 客户端:加 scope + CRUD + 视图;写各段 + 读回处置。
7. 编排:夜跑/增量 = 读 → 算 → 写 Bitable + 写 HubSpot 名单法视图 + 建 Task + 夜报;回复 triage 一天多跑。
8. core 维护 nudge(T0 45 / T1 90 / T2 60,读 Last Activity Date)。

### E. 本次落地(2026-08-05,沙箱已跑绿的纯逻辑核心)
- ✅ `outreach_state.py` **v2 重写**:废 `ACTIVE_DAYS=14` → 轮次(发送日期聚簇反推)+ 双时钟(节奏钟 `ROUND_DUE_DAYS=60` 催发 / 死线钟 `decay_stage` 透传);状态 not_started/sequencing/round_due/exhausted/replied;公司 auto-sequence 按"前置大空档 >`COMPANY_SEQ_GAP_DAYS=120`"识别为系统轮剔除(`analyze_rounds`)。view 中文名不变,`view_config` 无需改。
- ✅ `account_grading.core_tier()`:won 数 + open + list_quality → T0/T1/active/T2/not_core/review(**新增,未动现有 reconcile 机器**)。维护间隔按 tier(`core_maintenance_due(tier=...)`)。
- ✅ `breeze_outreach.build_prompt(name, website=)`:开头加"直接指定+禁反问"句消歧 + 可选注入 domain;新增纯函数 `looks_like_disambiguation()`(供安全网识别复核问句)。
- ✅ `account_reader`:`COLUMN_CANDIDATES` + `_parse_raw` 加 **Decay Stage / Won 数 / Company domain**(可选列,不进 REQUIRED,老视图不受影响)。
- ✅ 单测全绿(沙箱):`test_outreach_state`(v2 重写)、`test_customer_loop_v2`(core_tier+prompt+reader)、`test_view_pipeline`(fixture 按 v2 改)、连带 `test_account_grading/reader/reply_classify` 未回归。
- store 是 schemaless JSON,新字段(rounds/decay_stage)随 state dict 自动落;tier/list note/处置态 待 orchestration + Bitable 批次接。
- ✅ **Won 走 Breeze**:`breeze_outreach.build_deal_prompt` / `parse_deal_summary` / `_has_deal_json` / `ask_deal_summary`(复用消歧安全网;`_send_and_poll` 泛化出 `data_ready` 谓词支持不同 JSON 形状)。**✅ Decay Stage 推导**:`outreach_state.derive_decay_stage`。均沙箱单测绿(deal prompt/解析、decay 推导);`ask_deal_summary` 的实盘驱动待你真机验。
- ✅ **实盘校准(2026-08-05,Ned 真机跑 Breeze)**:①消歧误判修复 —— guard 措辞去掉 "several companies" 等自我命中词 + 检测器要求真问号/`please specify`(不再被自身 prompt 回显误判);②**输入丢首字符** —— `_submit` 改用 `inp.press_sequentially`(先聚焦再逐字),根除竞态。均在真机确认:精确名不再触发反问。
- ✅ **实盘通过(2026-08-05,真实账户)**:outreach 抽取 `breeze "Welotec"` → found=True / 9 邮件聚成 3 联系人×3 封 / 岗位 / attempts=1;deal 计数 `deal "Technoswitch"` → won=2 lost=0 open=0 → core_tier=T1。`ask_breeze` + `ask_deal_summary` 两条浏览器驱动链路真机验证通过。
- ✅ **回复判断归位到判断步(2026-08-05,Welotec/OOO 教训 + Ned 纠偏)**:Jos Zenner 的"回信"实为休假自动回复,Breeze 原把它当真回信 → 账户被 outreach 层的 `replied` flag 直接翻成"已回复"。**架构纠正**:outreach 抽取只报【事实:有 inbound 回来】不判真假(`build_prompt` 改成"report the fact... do not judge");**"是不是真回复(OOO/none vs 真回复)"的判断只在 `reply_classify`**(已加"仅 OOO/自动回复 → none")。
  - 状态机 `account_outreach_state` 加 `reply_is_real` 参数:检测到 inbound 且未判 → **`reply_pending`("待分类回复"provisional)**;编排跑 `reply_classify` 定局 → 真回复→`replied`;判假(OOO/none)→ 当没回复、**回落 sequencing/轮次**。`replied` flag 不再终局决定状态。单测覆盖三种。
- ✅ **两步回复分类真机验过(2026-08-07,Welotec/OOO)**:步骤① Breeze 抽到 Jos 的 OOO 原文 `{contact:Jos Zenner, reply_date, reply_text:"...I am not available until June 9th..."}`;步骤② 贾维斯 LLM 判成 **none** → 非真回复、不生成提议、Welotec 不进"已回复"。修了两个坑:①extract prompt 原来那句 "ignore automated" 让 Breeze 预过滤掉 OOO → 改成"连 OOO 也原样抓回、不预过滤";②`classify_reply_text` 的 `asyncio.run` 在浏览器事件循环里被吞成 None → 改成【独立线程 + 新事件循环】跑。LLM 分类器单独验也对(`interested_later`)。
- ✅ **回复分类改两步(Ned 选 B,2026-08-05):Breeze 抽事实 → 贾维斯 LLM 判**。`reply_classify` 重写:①`build_extract_prompt`/`parse_extract` 让 Breeze 只吐回信事实 `{contact, reply_date, reply_text}`(不判类别);②`build_classify_prompt` + `classify_reply_text`(走 `core.llm`,失败/none 一律 None = fail-safe 无提议)由**贾维斯自己的 LLM** 判 6 类。判断权 + 6 类逻辑握在贾维斯,Breeze 统一只当事实抽取器。约定:`classify()` 返回 dict ⇒ 真回复(reply_is_real=True);None ⇒ 无 inbound / none / OOO(reply_is_real=False)。纯层(prompt/parse)单测绿;LLM+Breeze 集成待真机验。
- ✅ **数据质量:导入缺名(account name = `--`)剔除** —— `view_manager.is_valid_account_name` / `nameless_accounts`;`split_accounts` 剔除缺名,`ask_breeze`/`ask_deal_summary` 缺名早退(error=invalid_account_name)。缺名账户无法进 view(名单法按名),须单独上报让 Ned 补名/重导(编排层接入夜报/Bitable"数据质量"视图)。

### F. 本批也落了(代码写好,browser/Breeze 侧需你真机验)
- ✅ `ask_breeze` 接消歧安全网:`_send_and_poll` 识别复核问句 → 自动回"用精确名"一次 → 仍问则 `status=disambiguation`;`ask_breeze` 据此返回 `error="needs_disambiguation"`(**不静默返空**)。新增 `website=` 参数注入 domain。
- ✅ `reply_classify.build_prompt` 加同款"精确名 / 禁反问"句(它也驱动 Breeze)。
- ⚠ 待你真机验:上述 Breeze 侧运行行为;`account_reader` 新列 DOM 实读(先去 HubSpot 视图 Edit columns 加 Decay Stage / Number of closed won deals / Company domain name)。

### G. 待你拍板 / 需先探测
- ✅ **回复分类 taxonomy(已定+已改绿)**:8 类按"类别=动作"归并成业务 **6 类**(见 §14),`reply_router` 重写(去 priority,三合一 `interested_later`,动作重定向 v2 字段)、`reply_classify` 类别指引改 6 类、三个相关单测改绿。不加 unclear。
  - 遗留:下游 `reply_path`/`cold_start`/`nightly` 仍有写 priority 的分支,现对新提议**优雅退化为 no-op**(priority 本就要下线);priority 全面退休 = 单独清理批(附一已列)。
- **编排层防误标**:`view_manager.compute_segments` 现把空 contacts 一律存 not_started;要改成遇 `ask_breeze` 的 `error`(needs_disambiguation 等)【跳过、不持久化】—— 改 `_ask` 边界,连实盘一起验。
- **Bitable 客户端 + 编排**:需先在你真机用真凭据【探测 lark-oapi bitable API 的实际返回形状】再写(盲写会牺牲准确性);随后接编排(读→算→写 Bitable + 名单法写 HubSpot 视图 + 建 Task + 夜报)。
