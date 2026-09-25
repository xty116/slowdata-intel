"""本地嵌入层：BGE-M3（fastembed/ONNX），语义去重用。

首次调用自动下载模型（约 570MB）；不可用时返回 None，上层回退到标题相似度。
"""
from __future__ import annotations

import threading

import numpy as np

from .config import CONFIG

_model = None
_lock = threading.Lock()
_model_failed = False


def get_model():
    global _model, _model_failed
    if _model is None and not _model_failed:
        with _lock:
            if _model is None and not _model_failed:
                try:
                    from fastembed import TextEmbedding

                    name = CONFIG["dedup"].get("embedding_model", "BAAI/bge-m3")
                    print(f"[embedding] 加载本地模型 {name}（首次运行需下载，请耐心等待）...")
                    _model = TextEmbedding(model_name=name)
                    print("[embedding] 模型就绪")
                except Exception as e:  # noqa: BLE001
                    print(f"[embedding] 模型不可用，将回退标题相似度去重: {e}")
                    _model_failed = True
    return _model


def embed(texts: list[str]) -> np.ndarray | None:
    """批量嵌入，返回 L2 归一化的 float32 矩阵；不可用返回 None。"""
    m = get_model()
    if m is None or not texts:
        return None
    try:
        vecs = list(m.embed(texts))
        mat = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms
    except Exception as e:  # noqa: BLE001
        print(f"[embedding] 嵌入失败，回退标题相似度: {e}")
        return None


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def cluster_by_similarity(
    items: list[dict], embs: np.ndarray | None, threshold: float
) -> list[list[int]]:
    """把 items 按语义相似度聚成簇（并查集，返回簇内下标列表）。"""
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    if embs is not None:
        for i in range(n):
            for j in range(i + 1, n):
                if cosine(embs[i], embs[j]) >= threshold:
                    union(i, j)
    else:
        import difflib

        fb = CONFIG["dedup"].get("title_fallback_threshold", 0.85)
        for i in range(n):
            ti = (items[i].get("title") or "").lower()
            for j in range(i + 1, n):
                tj = (items[j].get("title") or "").lower()
                if ti and tj and difflib.SequenceMatcher(None, ti, tj).ratio() >= fb:
                    union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())
