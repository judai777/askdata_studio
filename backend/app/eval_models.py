"""实时评测可选的远程 Chat 模型。"""
from __future__ import annotations

import os
import re
from dataclasses import replace

from .config import Settings, settings


MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}\Z")


def allowed_models() -> list[str]:
    names = [settings.llm_model, *(name.strip() for name in os.getenv("EVAL_LLM_MODELS", "").split(",") if name.strip())]
    if len(names) > 11 or any(not MODEL_NAME.fullmatch(name) for name in names):
        raise ValueError("EVAL_LLM_MODELS 最多登记 10 个合法模型名，每个模型名最多 80 字符")
    return list(dict.fromkeys(names))


def model_catalog() -> dict[str, object]:
    return {"default": settings.llm_model, "models": allowed_models(), "api_key_configured": bool(settings.api_key)}


def resolve_model(mode: str, model: str | None) -> Settings | None:
    if mode == "demo":
        if model is not None:
            raise ValueError("离线演示不能选择远程模型")
        return None
    selected = model or settings.llm_model
    if selected not in allowed_models():
        raise ValueError("远程模型未登记在评测白名单中")
    return replace(settings, llm_model=selected)
