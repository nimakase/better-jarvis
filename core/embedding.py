"""
core/embedding.py — 本地文本嵌入（L4 情节记忆语义检索的底座）

设计：
- 优先用本地 fastembed（onnxruntime 推理，模型权重下载到本地缓存后离线可用，
  文本不出机器）。默认多语模型 intfloat/multilingual-e5-small，中英混排都能编码。
- 若 fastembed 不可用（未安装 / 首次没联网下载模型），自动降级为
  「字符 3-gram 哈希向量」：纯本地、零依赖、确定性。语义精度弱一些，
  但保证系统始终可用、可离线、可测试，绝不因为缺模型而报错。
- 对外只暴露 embed / embed_one / cosine_topk；上层（episodic）不关心用了哪种后端。

隐私：两种后端都在本机完成，记忆文本绝不外发（这是选「本地嵌入」的初衷）。

维度说明：不同后端维度不同（fastembed 384 / 哈希 256）。episodic 会随行记录
生成时的维度，检索时只比对同维度的行，避免换后端后向量错配。
"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Sequence

import numpy as np

# 可用环境变量覆盖模型名；留默认即可。
_MODEL_NAME = os.environ.get("JARVIS_EMBED_MODEL", "intfloat/multilingual-e5-small")
_FALLBACK_DIM = 256

_lock = threading.Lock()
_backend = None   # None=未初始化；("fastembed", model) 或 ("hash", None)


def _init_backend():
    """惰性初始化嵌入后端，只做一次。任何异常都降级到哈希后端。"""
    global _backend
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is not None:
            return _backend
        try:
            from fastembed import TextEmbedding
            model = TextEmbedding(model_name=_MODEL_NAME)
            _backend = ("fastembed", model)
        except Exception:
            _backend = ("hash", None)
        return _backend


def backend_name() -> str:
    """返回当前实际使用的后端名：'fastembed' 或 'hash'。"""
    return _init_backend()[0]


def _hash_embed(text: str, dim: int = _FALLBACK_DIM) -> np.ndarray:
    """字符 3-gram 哈希词袋，L2 归一化。确定性、离线、无第三方依赖。"""
    vec = np.zeros(dim, dtype=np.float32)
    t = (text or "").strip().lower()
    if not t:
        return vec
    grams = [t[i:i + 3] for i in range(len(t) - 2)] or [t]
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
    n = np.linalg.norm(vec)
    return (vec / n).astype(np.float32) if n > 0 else vec


def _prefix(texts: Sequence[str], kind: str) -> list[str]:
    """e5 系列建议给查询/文档分别加 query:/passage: 前缀，能提升检索质量。"""
    if "e5" in _MODEL_NAME:
        p = "query: " if kind == "query" else "passage: "
        return [p + (t or "") for t in texts]
    return [t or "" for t in texts]


def embed(texts: Sequence[str], kind: str = "passage") -> np.ndarray:
    """把一批文本编码成 (n, dim) 的【已 L2 归一化】矩阵。

    kind: 'query'（检索时的查询）或 'passage'（入库的文档）。
    """
    texts = [t or "" for t in texts]
    name, model = _init_backend()
    if name == "fastembed":
        try:
            arr = np.array(list(model.embed(_prefix(texts, kind))), dtype=np.float32)
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            return (arr / norms).astype(np.float32)
        except Exception:
            pass  # 运行期失败也降级到哈希，绝不抛给上层
    return np.vstack([_hash_embed(t) for t in texts]).astype(np.float32)


def embed_one(text: str, kind: str = "passage") -> np.ndarray:
    return embed([text], kind=kind)[0]


def cosine_topk(query_vec: np.ndarray, matrix: np.ndarray, k: int):
    """query_vec:(dim,)、matrix:(n,dim) 均已归一化。返回 [(idx, score)] 按分降序 top-k。"""
    if matrix.size == 0 or k <= 0:
        return []
    sims = matrix @ query_vec
    k = min(k, len(sims))
    idx = np.argpartition(-sims, k - 1)[:k]
    idx = idx[np.argsort(-sims[idx])]
    return [(int(i), float(sims[i])) for i in idx]
