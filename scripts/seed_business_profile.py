"""scripts/seed_business_profile.py — 把 3 条精简业务事实写进贾维斯【常驻用户档案】(core memory)。

让贾维斯平时聊别的也知道 Ned 的业务。刻意只放 3 条蒸馏事实(常驻每轮注入,不能撑大);
完整业务见 docs/业务逻辑与工作流程.md(那份只在客户循环 LLM 判断时按需挂,不进常驻)。

    python -m scripts.seed_business_profile

幂等:add_fact 对完全相同文本去重(只刷新确认时间)。改了措辞想替换旧的,去设置面板删旧条。
"""
from __future__ import annotations

from core import profile

FACTS = [
    "Ned 在 CCL(电子元件 excess 收购/经销,'Your Excess Inventory Partner')做采购/Account Manager:"
    "客户=手上有多余板级电子元件要出的公司,CCL 收来转卖;能做板级电子元件,线材/塑料/五金/成品不做。",

    "客户循环两层分类:HubSpot Account Type(Core/Prospecting,公司政策+Ned 手设,决定是否被 time-decay 回收)"
    "vs 贾维斯价值分层(Core 内 T0 多won/T1 低频/T2 lost料好;Prospecting 未开发/开发中/待处理/已回复)。"
    "贾维斯只读不写 Account Type/owner。",

    "冷开发 sequence 是 Ned 自己发:3 轮×3 封同标题、轮间隔 2 月;三轮无果换人或放弃(放弃=让 time-decay 自然回收);"
    "回复由贾维斯 LLM 判 6 类。完整业务见 docs/业务逻辑与工作流程.md。",
]


def main() -> int:
    print("写入常驻用户档案(core memory)…")
    for f in FACTS:
        r = profile.add_fact(f, evidence="业务记忆 seed(docs/业务逻辑与工作流程.md)")
        print(f"  [{'ok' if r.get('ok') else 'skip'}] {r.get('message')}  · {f[:24]}…")
    print("\n✅ 完成。贾维斯现在平时聊天也带这 3 条业务背景;完整业务是客户循环判断时按需挂的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
