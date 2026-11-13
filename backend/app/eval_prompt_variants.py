"""Code-reviewed prompt additions available only to evaluation runs."""
from __future__ import annotations

import hashlib


PROMPT_VARIANTS = {
    "default": "",
    "time_filter_v1": (
        "在生成 SQL 前，逐项核对用户问题中的时间范围。"
        "对每个参与计算的事实表分别应用其要求的日期过滤；"
        "若目标表另有目标月份，也分别过滤目标月份。"
        "不要从结果展示月份推断事实表已被过滤。"
    ),
}


def prompt_variant_text(variant: str) -> str:
    try:
        return PROMPT_VARIANTS[variant]
    except KeyError as error:
        raise ValueError("未登记的评测 Prompt 版本") from error


def prompt_variant_catalog() -> list[dict[str, str]]:
    return [
        {"id": variant, "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]}
        for variant, text in PROMPT_VARIANTS.items()
    ]
