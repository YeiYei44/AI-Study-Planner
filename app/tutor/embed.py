"""Local embeddings via fastembed (ONNX runtime, no torch) — always
local and free regardless of which chat backend answers questions. Groq
(this project's default chat backend) has no embeddings endpoint at all,
and embedding is cheap enough on CPU to never need a paid API for it.

Verified directly (not assumed): loads in ~2.5s, embeds in milliseconds,
and correctly ranks semantically related sentences above unrelated ones
on the default model.
"""

from __future__ import annotations

import struct

import numpy as np

MODEL_NAME = "BAAI/bge-small-en-v1.5"  # 384-dim, ~67MB, lazy-downloaded on first use

_model = None  # process-wide singleton — loading takes real time, don't repeat it


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return [e.tolist() for e in _get_model().embed(texts)]


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]


def serialize_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def deserialize_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / denom) if denom else 0.0
