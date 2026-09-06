"""
Web Push —— OS 级通知推送。

  - 首次启动自动生成 VAPID 密钥对（存 DATA_DIR/vapid.json，私钥不入代码）
  - /api/push/vapid-public  前端订阅用的公钥
  - /api/push/subscribe     保存浏览器订阅
  - send_push(title,content) 用 pywebpush 签名发送，注册为 core.delivery 的 "webpush" 渠道

依赖：cryptography（已用）、pywebpush（需 `pip install pywebpush`）。
缺 pywebpush 或 VAPID 时优雅降级（不崩，记日志）。
"""
import base64
import json
import logging
import os

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

import config

logger = logging.getLogger("jarvis.push")
router = APIRouter()

VAPID_PATH = config.DATA_DIR / "vapid.json"
SUBS_PATH = config.DATA_DIR / "push_subscriptions.json"
# VAPID 联系人（推送服务用于联系发送方，规范要求是能联系到站点所有者的真实地址）。
# 不硬编码真实邮箱进源码（随 git 泄露）——从 env 读，本机部署时在 .env 设
# JARVIS_VAPID_SUBJECT=mailto:you@example.com；未设置则用占位符（功能不受影响，
# 只影响推送服务出问题时联系不到你）。
VAPID_SUBJECT = os.environ.get("JARVIS_VAPID_SUBJECT", "mailto:admin@example.com")


def _gen_vapid() -> dict:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    raw = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    app_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"private_pem": priv_pem, "public_key": app_key, "subject": VAPID_SUBJECT}


def ensure_vapid():
    if VAPID_PATH.exists():
        try:
            return json.loads(VAPID_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        v = _gen_vapid()
        VAPID_PATH.write_text(json.dumps(v, indent=2), encoding="utf-8")
        logger.info("已生成 VAPID 密钥 → %s", VAPID_PATH)
        return v
    except Exception as e:
        logger.warning("VAPID 生成失败（WebPush 暂不可用）：%s", e)
        return None


_VAPID = ensure_vapid()


def _load_subs() -> list:
    if SUBS_PATH.exists():
        try:
            return json.loads(SUBS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_subs(subs: list) -> None:
    SUBS_PATH.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/api/push/vapid-public")
async def vapid_public():
    return JSONResponse({"key": _VAPID["public_key"] if _VAPID else ""})


@router.post("/api/push/subscribe")
async def subscribe(sub: dict = Body(...)):
    subs = _load_subs()
    ep = sub.get("endpoint")
    if ep and not any(s.get("endpoint") == ep for s in subs):
        subs.append(sub)
        _save_subs(subs)
    return JSONResponse({"ok": True, "count": len(subs)})


def send_push(title: str, content: str, url: str = "/") -> dict:
    """给所有订阅发推送；自动清理失效订阅。注册为 delivery 的 webpush 渠道。"""
    if not _VAPID:
        return {"ok": False, "reason": "VAPID 未配置"}
    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        return {"ok": False, "reason": f"pywebpush 未安装：{e}"}

    subs = _load_subs()
    payload = json.dumps({"title": title, "body": (content or "")[:300], "url": url})
    sent, dead = 0, []
    for s in subs:
        try:
            webpush(subscription_info=s, data=payload,
                    vapid_private_key=_VAPID["private_pem"],
                    vapid_claims={"sub": _VAPID["subject"]})
            sent += 1
        except WebPushException as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code in (404, 410):
                dead.append(s.get("endpoint"))   # 订阅已失效
            else:
                logger.warning("push 失败：%s", e)
        except Exception as e:
            logger.warning("push 异常：%s", e)
    if dead:
        _save_subs([s for s in subs if s.get("endpoint") not in dead])
    return {"ok": True, "sent": sent, "removed": len(dead)}


def _channel(title: str, content: str):
    return send_push(title, content)


# 注册为 delivery 的 webpush 渠道（导入即注册）
try:
    from core import delivery
    delivery.register_channel("webpush", _channel)
except Exception as e:  # 不应阻断启动
    logger.warning("注册 webpush 渠道失败：%s", e)
