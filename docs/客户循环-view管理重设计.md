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

**计数底座 = 联系人层**(Breeze 数据逐封带联系人 + 岗位,聚合几乎白捡,且消除"轮"的歧义):每联系人 → 收几封 / 是否回复 / 岗位。**一个联系人 3 封无回复 = 该联系人到顶(一轮)**。账户状态全部由此推导。另读账户**全部关联联系人**,算"还有几个没试过的";**岗位**让换人建议更准(如提示"还有个采购/决策角色没碰过")。换不换、换谁仍是 Ned 定,贾维斯只摊牌。

**Prospecting(无 deal)—— 冷开发流:**

| view | 含义 | 动作 |
|---|---|---|
| 未开发 | 无 outreach 记录 | 待启动第一轮 |
| 开发中 | 有 sequence 在跑(HubSpot 自动发) | 不用管 |
| 1 个联系人到顶·无回复 | 首个联系人 3 封没回 | 可发第 2 个(换不换/换谁 Ned 定,视图列已试/未试联系人+岗位) |
| 到顶 | 2 个联系人各跑满 3 封、都无回复 | Ned 决策:挖新联系人 / 收手(视图标"还剩 N 个没试过") |
| 已回复·待跟进 | 有人回信 | sequence 自动停;约了时间就建 Task,移出冷开发 |

**Core(有 deal)—— 维护流,绝不冷开发:**

| view | 含义 | 动作 |
|---|---|---|
| 维护到点 | 距上次 touch > 2 个月 | 提醒 Ned 联系 |
| Core·冷处理 | 低质(线材塑料头那类),手动钉休眠 | 搁置,有机会再看 |

## 5. 参数(已定)

- 1 轮 = **3 封**同标题邮件
- 轮 = **单个联系人**的 3 封;到顶 = **2 个联系人**各跑满 3 封、都无回复
- Core 维护间隔 = **2 个月(60 天)**未 touch
- 换联系人 = **不自动**,Ned 判断(贾维斯只报状态)

## 6. 待确认 / 风险

1. ~~「轮」的口径~~ **已定**:轮 = 单个联系人的 3 封;计数落在联系人层,账户状态往上推(见 §4 计数底座),歧义消除。
2. **名单法精度**:`contains exactly` 是子串 + 多值 OR,须用够独特的全名;值数量可能有上限(几百个 prospecting 时验)。`Record ID` 的 `is any of` 是否更精确,待验。Set values 后是否需额外点 Save 才持久化,待验。
3. **贾维斯批量驱动 Breeze**:逐账户开 Copilot 问 + 解析 JSON;增量下量可控,冷启动慢、可分批。
4. **store schema**:per-account `{contacts:[{name, job_title, 邮件数, 各封日期, 回复?}], 全部关联联系人名单, 到顶?, 维护 last_touch, 冷处理 flag}`。

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
