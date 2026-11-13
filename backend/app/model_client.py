from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from threading import Lock
from typing import Any, Callable

from .config import Settings, settings


class ModelRequestBudgetExceeded(Exception):
    """评测运行达到远程 HTTP 请求上限。"""


class ModelClient:
    """Chat、Embedding 和 Rerank 接口客户端。"""

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or settings
        self._blocked_until: dict[str, float] = {}
        self.telemetry_enabled = False
        self.telemetry: list[dict[str, Any]] = []
        self.current_stage: str | None = None
        self.request_budget_limit: int | None = None
        self.request_count = 0
        self.request_budget_exhausted = False
        self._budget_lock = Lock()
        self.on_request_reserved: Callable[[int], None] | None = None

    def configure_request_budget(self, limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("远程请求上限必须在 1 到 100 次之间")
        with self._budget_lock:
            self.request_budget_limit = limit
            self.request_count = 0
            self.request_budget_exhausted = False

    def _reserve_request(self) -> None:
        with self._budget_lock:
            if self.request_budget_limit is not None and self.request_count >= self.request_budget_limit:
                self.request_budget_exhausted = True
                raise ModelRequestBudgetExceeded("远程请求预算已耗尽")
            self.request_count += 1
            count = self.request_count
        if self.on_request_reserved:
            self.on_request_reserved(count)

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key)

    def chat(self, system: str, user: str, *, temperature: float | None = None) -> str:
        payload = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "stream": False,
        }
        result = self._post(self.config.chat_url, payload)
        try:
            return str(result["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("大模型响应缺少 message.content") from exc

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.chat(system, user)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if not match:
                raise RuntimeError("大模型未返回有效 JSON")
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise RuntimeError("大模型未返回有效 JSON") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 10):
            payload = {
                "model": self.config.embedding_model,
                "input": texts[start : start + 10],
                "dimensions": self.config.embedding_dimensions,
                "encoding_format": "float",
            }
            result = self._post(self.config.embeddings_url, payload)
            items = sorted(result.get("data", []), key=lambda item: item.get("index", 0))
            vectors.extend(self._normalize(item["embedding"]) for item in items)
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding 返回数量与输入不一致")
        return vectors

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        payload = {
            "model": self.config.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "instruct": "Rank database schema fields by relevance to the user's analytics query.",
        }
        result = self._post(self.config.rerank_url, payload)
        output = [
            (int(item["index"]), float(item.get("relevance_score", item.get("score", 0))))
            for item in result.get("results", [])
        ]
        return sorted(output, key=lambda item: item[1], reverse=True)

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.api_key:
            raise RuntimeError("LLM_API_KEY 尚未配置")
        if time.monotonic() < self._blocked_until.get(url, 0):
            raise RuntimeError("模型接口暂时处于30秒熔断窗口，请稍后重试")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                self._reserve_request()
            except ModelRequestBudgetExceeded:
                if attempt:
                    self._record_telemetry(url, payload, started, attempt, False, None)
                raise
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                    self._record_telemetry(url, payload, started, attempt + 1, True, result.get("usage"))
                    return result
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = RuntimeError(f"模型接口返回 HTTP {exc.code}: {detail}")
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.config.max_retries:
                time.sleep(0.4 * (2**attempt))
        # 网络请求失败后对当前接口启用短时熔断。
        self._blocked_until[url] = time.monotonic() + 30
        self._record_telemetry(url, payload, started, attempt + 1, False, None)
        raise RuntimeError(f"模型接口调用失败: {last_error}") from last_error

    def _record_telemetry(
        self, url: str, payload: dict[str, Any], started: float,
        attempts: int, success: bool, usage: Any,
    ) -> None:
        if not self.telemetry_enabled:
            return
        kind = "chat" if url == self.config.chat_url else "embedding" if url == self.config.embeddings_url else "rerank"
        safe_usage = {
            key: value for key, value in (usage if isinstance(usage, dict) else {}).items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
            and isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
        self.telemetry.append({
            "kind": kind,
            "stage": self.current_stage,
            "model": str(payload.get("model") or ""),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            "attempts": attempts,
            "success": success,
            "usage": safe_usage or None,
        })

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [float(value) / norm for value in vector]
