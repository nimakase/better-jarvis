# 工具创建框架重设计（Tool Authoring Redesign）

> 状态：设计 + 分期实施中 · 起草 2026-07-16
> 目标：从根源上消除"自建工具造不好"的一整类问题，让贾维斯**自己**能把工具写对。
> 关联事实源：`core/tool_builder.py`（生成/校验/激活）、`core/skill_policy.py`（导入与复用策略）、
> `core/registry.py`（注册表）、`core/self_iteration.py`（可借的测试门/子进程/回滚机制）、
> `connectors/self_inspect.py`（`read_self_source` 只读自省）。

## 1. 问题

自建工具（`skills/<名>/tool.py`）反复出现"造不好、修不好"，典型案例 `oem_ems_screener`：
模型凭空写出 `HubSpotBrowser.create()/fetch_companies_from_view()/close()`——这些方法在真实的
`prospecting/hubspot_worker.py` 里**零命中**（真实类是 `HubSpotBrowser(paths, logger)` +
`start/stop/login_bootstrap`）。代码能通过静态校验、能激活，却在真正运行时 AttributeError。
让模型"再写一遍"无用——每遍都在重新幻觉一套看似合理、实则不存在的 API。

在此之前还接连暴露过：生成被 token 上限截断（结尾 `}` 丢失）、注册表首次登记后无法刷新、
配置（密钥/模型）对技能不可见、没有"存我确切代码"的入口等。

## 2. 根因（一句话）

**造工具的范式是"盲写一次性文本"，而不是"像开发者那样迭代"。**
`create_tool(描述)` = 一次固定提示词的 LLM 调用 → 只做 AST 静态检查 → 存草稿 → 人工激活。
它**没读过要复用的真实代码、没真正跑过一次、错了没有基于真实报错重来**。
上面每个 bug 都是这个范式的症状：

- 看不见真实 API → 幻觉、无法复用 building block；
- 只有静态检查、从不运行 → 幻觉代码"通过校验"直到真用时才炸；
- 一次性整文件生成 → 超长即截断；
- 生成与贾维斯的 agentic 能力（读代码、跑、改）完全脱节。

## 3. 目标范式：可验证、有上下文的"写作循环"

贾维斯造工具应当和他做别的事一样：**读真实 API → 写 → 跑一下 → 按真实报错改 → 通过了再交**。
四根支柱：

1. **看得见（Discovery）**：写之前能看到要复用的 building block 的真实签名；需要什么能力先确认存不存在。
2. **跑得过（Verification）**：静态校验之外，在**隔离子进程**里冒烟测试——import 模块、`TOOL_DEF` 能加载、
   handler 存在、能用代表性/mock 参数空跑一次。幻觉 API 在这一步就暴露，而不是等真用时。
3. **会改（Iteration）**：造工具是有界循环——生成 → 静态+冒烟验证 → 失败把**真实报错**喂回改 → 最多 N 轮；
   只把真正通过的工具交给用户，或诚实说"搞不定，原因是 X"。
4. **有边界（Honesty）**：读完发现所需能力不存在，就停下报告"building block 缺这个方法，得先加"，绝不臆造。
   任务分三类分别处理：**自包含** / **复用 building block** / **需要新建 building block 能力**。

## 4. 复用清单：多数零件已具备，主要是"编排"

| 能力 | 现状 |
|------|------|
| 看得见（读真实源码） | `connectors/self_inspect.read_self_source` 已存在（只读、带围栏） |
| 跑得过（测试门/子进程/回滚） | `core/self_iteration` 已有 `_run_script`（跑脚本看退出码）+ OPEN-only + 回滚，可借其机制 |
| 静态校验 | `core/tool_builder.validate_tool_code`（AST：禁危险 import、SQL 注入、结构检查） |
| 注册表热替换 / 注销 | `core/registry.register_skill_tool(replace)` / `unregister`（2026-07-16 已补） |
| 配置对技能可见 | `config._export_env_for_skills()` 导出 os.environ（2026-07-16 已补） |
| 存确切代码 | 元工具 `update_tool_code`（2026-07-16 已补） |
| 截断防护 | `_call_codegen` 足额预算 + 截断检测重试（2026-07-16 已补） |
| **建 building block 清单 / 注入真实 API** | ✅ 一期已实现 |
| **API 一致性静态检查（抓臆造方法）** | ✅ 二期已实现 |
| **子进程 import 冒烟门** | ✅ 二期已实现 |
| **agentic 写作循环 / 造工具子 agent** | 三期（待做） |

唯一有分量的新工程是"**安全地跑一次冒烟测试**"：现在激活在主进程 `exec`，拿未验证的生成代码在主进程空跑有风险，
必须放到**子进程**隔离跑。这是二期的重点。

## 5. 分期方案

### 一期（便宜、立刻止血）——让盲生成器"看得见"且"诚实"

- `core/skill_policy.py` 新增**可复用 building block 清单** `BUILDING_BLOCKS`（模块 → 描述 + 关键符号），
  并把这些模块从"白名单外 import"里**放行**（不再警告/劝退）。
- 用 AST 从这些模块**抽取真实公共签名**（类/方法/函数的参数，不含函数体），渲染成一段权威 API 参考，
  注入 `CODE_GEN_PROMPT`。→ 模型据此写对，`HubSpotBrowser.create()` 这类幻觉从源头消失。
- `CODE_GEN_PROMPT` 加**边界声明**：你看不到、也不能臆造 app 的内部模块 API；只能复用清单里列出的
  building block，并以注入的签名为准；若所需能力在这些 building block 里不存在，**明确说"需要先给 building
  block 加这个能力"并停手**，不要编。
- `validate_tool_code`：building block 的 import 视为**已许可**（不再报"白名单外"警告）。

一期不改变"一次性生成"这一点，但通过**把真实 API 喂进提示词**，让单次生成也能写对复用代码——这是当前失败的主因。

### 二期（关键）——运行时验证门 ✅ 已实现（2026-07-16）

实现落点：`skill_policy.check_building_block_usage`（AST 一致性检查，纳入 `validate_tool_code` 阻断级）
+ `tool_builder.smoke_import_skill`（`activate_skill` 激活前的子进程 import 冒烟）。以下为原始设计：


- 新增在**子进程**里对草稿做冒烟测试：import 模块 → 断言 `TOOL_DEF` 结构与 handler 存在 →
  （可选）用代表性/mock 参数对 handler 做一次 dry-run，超时/异常/AttributeError 即判失败并带回真实报错。
- 冒烟不过的工具**不放行激活**（或明确标注"运行时未通过"）。借 `self_iteration` 的子进程跑法。
- 安全：子进程限制资源与超时；对有副作用的工具（开浏览器/连 HubSpot）用 dry-run 契约或跳过实际副作用。

### 三期——agentic 写作循环 / 造工具子 agent

- 把 `create_tool`/`edit_tool` 从"单次补全"升级为**有界循环**：生成 → 静态+冒烟 → 失败喂回真实报错 → 改，N 轮。
- 更彻底：一个专门的"**造工具子 agent**"，手握 `read_self_source` + 校验器 + 子进程冒烟测试，
  自己去读它需要的任何 building block 源码（不止清单内），像开发者一样把工具做出来。

## 6. 安全考量

- **building block 放行是能力授予**：`prospecting.*` 会开浏览器、用 HubSpot 凭据。缓解：技能激活前仍需人工审查代码；
  清单只列**确需被技能复用**的模块；二期的冒烟测试对有副作用者用 dry-run。
- **子进程冒烟**：必须资源/超时/网络受限，且不得触发真实副作用（下单/发消息/改数据）。
- **边界不变量保留**：技能仍**禁止** import `core/connectors/config/main`；仍不能覆盖第一方工具（registry origin 保护）。
- 三期的造工具子 agent 若能读任意源码，注意不要把敏感文件（`.env`/`vault`）纳入可读范围——沿用 `read_self_source` 既有围栏。

## 7. 未决问题

- 冒烟测试对"有副作用工具"的 dry-run 契约怎么统一约定（工具需暴露一个可 mock 的入口？）。
- building block 清单的维护：手工列 vs 从某种标注自动发现。
- 三期子 agent 用主控模型还是更强模型；预算与延迟。
