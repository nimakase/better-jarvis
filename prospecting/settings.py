"""prospecting/settings.py — 客户循环(customer_loop)专属运行时配置。

2026-08-12:从 config.py 的全局 Settings 里迁出 customer_loop 相关的四个字段
(customer_loop_apply/grade_view_url/bitable_app_token/bitable_table_id)。诊断见项目记忆
[[jarvis-architecture-migration-plan]] ②——这几个字段以前混在核心 Settings 类里,跟
HOST/PORT/模型名这些真正意义上"中心进程"该管的配置平铺在一起;客户循环是第一方复杂 skill
（`prospecting/` 整个包），它的运行时参数该有自己的命名空间，不该反向占用中心进程的配置面。

跟 config.py 用**同一份** `.env` 文件（`env_file` 指向仓库根 `.env`），复用同一套
pydantic-settings 机制——只是把"归属"从核心搬到这个 skill 自己名下。`.env` 里的变量名
（`JARVIS_CUSTOMER_LOOP_APPLY`/`JARVIS_GRADE_VIEW_URL`/`BITABLE_APP_TOKEN`/`BITABLE_TABLE_ID`）
完全不用改，调用方原来靠 `config.CUSTOMER_LOOP_APPLY` 等名字读的值，现在改从这个模块读，
读到的还是同一个数字。

已确认（grep 全仓库）只有 `connectors/customer_loop_tools.py`（apply 开关 + 全字段视图 URL）
和 `prospecting/bitable_client.py`（驾驶舱 Bitable 凭据）两处消费这四个字段，`config.py` 里
的旧字段/常量已随本次改动一并删除，不留新旧并存的死代码。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).resolve().parent.parent  # jarvis 仓库根 —— 与 config.py 同一个 .env


class CustomerLoopSettings(BaseSettings):
    """类型化、可校验的客户循环配置。环境变量优先，其次读取项目根 .env（跟 config.Settings 一致）。"""
    model_config = SettingsConfigDict(
        env_file=str(_BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 客户循环夜间作业:apply=真写(默认 False=只读 dry-run)。
    customer_loop_apply: bool = Field(default=False, validation_alias="JARVIS_CUSTOMER_LOOP_APPLY")
    # 全字段源视图(读全书用,覆盖默认值见 connectors/customer_loop_tools.DEFAULT_VIEW_URL)。
    grade_view_url: str = Field(default="", validation_alias="JARVIS_GRADE_VIEW_URL")
    # 「驾驶舱」多维表格(Bitable)标识:scripts.bitable_bootstrap 建表后写进 .env。
    # 字段名不加前缀(跟历史 .env 变量名 BITABLE_APP_TOKEN/BITABLE_TABLE_ID 保持一致)。
    bitable_app_token: str = ""
    bitable_table_id: str = ""
    # Breeze outreach 查询要按【真实 CRM owner 姓名】过滤"谁发的邮件"（见
    # prospecting/breeze_outreach.build_prompt）——这个名字必须匹配你 HubSpot 账号的
    # 显示名才能查对人，所以不能是空字符串占位；但也不能硬编码进源码随 git 泄露
    # 真实姓名（教训：此前有真实姓名直接写死在这类文件里，仓库转 public 前才发现并改掉）。
    # 从 .env 的 JARVIS_CRM_OWNER_NAME 读；未配置时退回占位符，此时 Breeze 查询会
    # 查不到人、明确返回空结果，而不是悄悄查错人。
    crm_owner_name: str = Field(default="CRM Owner", validation_alias="JARVIS_CRM_OWNER_NAME")


_settings = CustomerLoopSettings()

# ── 向后兼容的模块级常量(调用方原来怎么用 config.X,现在就怎么用 settings.X)──────────
CUSTOMER_LOOP_APPLY = _settings.customer_loop_apply
GRADE_VIEW_URL = _settings.grade_view_url
BITABLE_APP_TOKEN = _settings.bitable_app_token
BITABLE_TABLE_ID = _settings.bitable_table_id
CRM_OWNER_NAME = _settings.crm_owner_name
