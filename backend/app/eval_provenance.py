"""评测运行的非敏感 Agent 实现快照。"""
from __future__ import annotations

import hashlib
from typing import Any, Sequence

from .config import BASE_DIR, Settings
from .eval_prompt_variants import prompt_variant_text


PROMPT_SOURCES = (
    "app/preprocessing.py",
    "app/querying/single_database_agent.py",
    "app/querying/data_qa_agent.py",
    "app/querying/response_generator.py",
    "app/mcp_runtime/server.py",
    "app/skills/database_query/SKILL.md",
    "app/skills/database_query/config.json",
    "app/skills/data_qa/SKILL.md",
    "app/skills/data_qa/config.json",
)

WORKFLOW_SOURCES = (
    "app/services/askdata_service.py",
    "app/services/session_context.py",
    "app/workflows/query_graph.py",
    "app/workflows/result_builder.py",
    "app/workflows/state.py",
    "app/retrieval/service.py",
    "app/retrieval/graph.py",
    "app/querying/duckdb_engine.py",
    "app/mcp_runtime/tools/database_tools.py",
    "app/skills/registry.py",
    "app/model_client.py",
    "app/database.py",
)


def source_hash(paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(paths):
        digest.update(name.encode("utf-8"))
        digest.update((BASE_DIR / name).read_bytes())
    return digest.hexdigest()[:16]


def _endpoint_hash(config: Settings) -> str:
    digest = hashlib.sha256()
    for name, value in (("chat", config.llm_base_url), ("embedding", config.embedding_base_url or config.llm_base_url), ("rerank", config.rerank_base_url)):
        digest.update(name.encode("utf-8"))
        digest.update(value.encode("utf-8"))
    return digest.hexdigest()[:16]


def provenance_snapshot(mode: str, config: Settings | None = None) -> dict[str, Any]:
    if mode == "demo":
        return {"kind": "demo", "prompt_hash": None, "workflow_hash": None, "config": None}
    if mode != "live" or config is None:
        raise ValueError("实时评测缺少模型配置")
    variant_text = prompt_variant_text(config.eval_prompt_variant)
    prompt_digest = hashlib.sha256()
    prompt_digest.update(source_hash(PROMPT_SOURCES).encode("ascii"))
    prompt_digest.update(config.eval_prompt_variant.encode("utf-8"))
    prompt_digest.update(variant_text.encode("utf-8"))
    return {
        "kind": "live",
        "prompt_hash": prompt_digest.hexdigest()[:16],
        "prompt_variant": config.eval_prompt_variant,
        "workflow_hash": source_hash(WORKFLOW_SOURCES),
        "config": {
            "llm_model": config.llm_model,
            "endpoint_hash": _endpoint_hash(config),
            "embedding_model": config.embedding_model,
            "embedding_dimensions": config.embedding_dimensions,
            "rerank_model": config.rerank_model,
            "temperature": config.temperature,
            "timeout": config.timeout,
            "max_retries": config.max_retries,
            "schema_recall_threshold": config.schema_recall_threshold,
            "bm25_top_k": config.bm25_top_k,
            "dense_top_k": config.dense_top_k,
            "rrf_top_k": config.rrf_top_k,
            "max_schema_fields": config.max_schema_fields,
            "mcp_max_tool_calls": config.mcp_max_tool_calls,
            "context_table_row_limit": config.context_table_row_limit,
            "route_context_turns": config.route_context_turns,
            "short_term_summary_enabled": config.short_term_summary_enabled,
            "short_term_summary_trigger_tokens": config.short_term_summary_trigger_tokens,
            "short_term_summary_batch_tokens": config.short_term_summary_batch_tokens,
            "short_term_min_recent_turns": config.short_term_min_recent_turns,
            "session_archive_enabled": config.session_archive_enabled,
        },
    }


def compare_provenance(baseline: dict[str, Any] | None, candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not baseline or not candidate:
        return {"status": "unknown", "baseline": baseline, "candidate": candidate, "changes": []}
    before = {"kind": baseline.get("kind"), "prompt_hash": baseline.get("prompt_hash"), "workflow_hash": baseline.get("workflow_hash"), **{f"config.{key}": value for key, value in (baseline.get("config") or {}).items()}}
    after = {"kind": candidate.get("kind"), "prompt_hash": candidate.get("prompt_hash"), "workflow_hash": candidate.get("workflow_hash"), **{f"config.{key}": value for key, value in (candidate.get("config") or {}).items()}}
    changes = [{"field": field, "before": before.get(field), "after": after.get(field)} for field in sorted(before.keys() | after.keys()) if before.get(field) != after.get(field)]
    return {"status": "changed" if changes else "same", "baseline": baseline, "candidate": candidate, "changes": changes}
