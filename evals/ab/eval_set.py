"""
步骤 2：标注评测集。每条 = 一个用户输入 + 期望工具集（正确答案）+ 场景类别。

expected_tools 用「真实工具名」（先跑 dump_tools.py 确认名字）。
  - 空集 []  表示「正确做法是不调任何工具」（闲聊/常识题，用来抓误召）。
  - 多个名字表示这一轮应同时/先后用到这些工具。

category 取值（与方案第 3 节对应）：
  distract  多干扰单目标 | hidden 隐藏工具诱惑 | domain 非核心组目标
  cross 跨域单轮 | core 核心即可 | none 无需工具

下面是种子用例：✅ 标注的工具名我较有把握；⚠️ TODO 的请按 dump_tools 的输出补/改名。
建议每个 category 凑到 5–8 条。
"""

CASES = [
    # ── core：只用核心工具，两臂应几乎一致（健全性校验）──────────────
    dict(id="core-1", category="core",
         prompt="帮我记一下：我的风险偏好是稳健型。",
         expected_tools=["write_memory"]),
    dict(id="core-2", category="core",
         prompt="我之前设的风险偏好是什么？",
         expected_tools=["query_memory"]),

    # ── none：不该调任何工具（抓误召 / B 是否乱 load_tools）────────────
    dict(id="none-1", category="none",
         prompt="用三句话解释一下什么是指数基金。",
         expected_tools=[]),
    dict(id="none-2", category="none",
         prompt="你好，今天有点累，随便聊聊。",
         expected_tools=[]),

    # ── domain：正确工具在某领域组，量化 B 的 load_tools 代价 ──────────
    # ⚠️ TODO: 把工具名换成 dump_tools 里 signal_intel / delivery / report 组的真实名
    dict(id="domain-intel-1", category="domain",
         prompt="扫一下今天的潜客信号。",
         expected_tools=["intel_scan"]),            # ⚠️ TODO 确认名
    dict(id="domain-delivery-1", category="domain",
         prompt="以后我休假期间日报和潜客都先别推。",
         expected_tools=["delivery_vacation_default"]),
    dict(id="domain-report-1", category="domain",
         prompt="生成一份本周的报告。",
         expected_tools=["generate_report"]),       # ⚠️ TODO 确认名

    # ── distract：正确工具小众、周围干扰多（B 应受益）─────────────────
    dict(id="distract-1", category="distract",
         prompt="读一下桌面上那个合同 PDF 的内容。",
         expected_tools=["read_document"]),         # ✅
    # ⚠️ TODO: 再补几条「正确答案是某个小众领域工具」的用例

    # ── hidden：用户要的功能其工具在未加载组（B 的主要风险点）─────────
    # 期望：B 应先 load_tools 发现该组，再调对应工具；记录是否臆造/放弃。
    dict(id="hidden-intel-1", category="hidden",
         prompt="把最近积累的客户信号整理成一份潜客清单。",
         expected_tools=["intel_scan"]),            # ⚠️ TODO 确认名/可能是多步

    # ── cross：一条消息要用两个组的工具 ───────────────────────────────
    dict(id="cross-1", category="cross",
         prompt="生成本周报告，然后把文件发到对话里给我。",
         expected_tools=["generate_report", "send_file_to_chat"]),  # ⚠️ TODO 确认报告工具名
]
