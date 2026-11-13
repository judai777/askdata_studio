"""实时评测可选的远程 Chat 模型。"""
from __future__ import annotations

import os
import re
from dataclasses import replace

from .config import Settings, settings
from .eval_prompt_variants import prompt_variant_catalog, prompt_variant_text
from .eval_grader_version import grader_hash


MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}\Z")


def allowed_models() -> list[str]:
    names = [settings.llm_model, *(name.strip() for name in os.getenv("EVAL_LLM_MODELS", "").split(",") if name.strip())]
    if len(names) > 11 or any(not MODEL_NAME.fullmatch(name) for name in names):
        raise ValueError("EVAL_LLM_MODELS 最多登记 10 个合法模型名，每个模型名最多 80 字符")
    return list(dict.fromkeys(names))


def model_catalog() -> dict[str, object]:
    return {"default": settings.llm_model, "models": allowed_models(), "prompt_variants": prompt_variant_catalog(),
            "grader_hash": grader_hash(), "api_key_configured": bool(settings.api_key)}


def resolve_model(mode: str, model: str | None, prompt_variant: str = "default") -> Settings | None:
    if mode == "demo":
        if model is not None or prompt_variant != "default":
            raise ValueError("离线演示不能选择远程模型或 Prompt 版本")
        return None
    prompt_variant_text(prompt_variant)
    selected = model or settings.llm_model
    if selected not in allowed_models():
        raise ValueError("远程模型未登记在评测白名单中")
    return replace(settings, llm_model=selected, eval_prompt_variant=prompt_variant)
