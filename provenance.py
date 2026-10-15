"""Local analysis provenance. No requests, chat text, credentials or database writes."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re

PINNED_BASELINE_MODEL = "jev-1.13.0"
CANDIDATE_MODEL = "jev-1.14.0"
AGGREGATION_VERSION = "aggregation-v2"
CONTEXT_VERSION = "turn-context-v2"
CACHE_VERSION = "analysis-cache-v2"


def versioned_model(model: str | None) -> bool:
    return bool(re.fullmatch(r"jev-\d+\.\d+\.\d+", model or ""))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def summarize(results: list[dict]) -> dict:
    """Check all successful answers, including cached answers, not just the first."""
    models: Counter = Counter()
    requested: Counter = Counter()
    recipes: dict[str, dict] = {}
    unknown = 0
    unversioned = 0
    legacy = 0
    for entry in results:
        if entry.get("error"):
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        model = result.get("model")
        if isinstance(model, str) and model:
            models[model] += 1
            unversioned += not versioned_model(model)
        else:
            unknown += 1
        recipe = result.get("analysis_provenance")
        if isinstance(recipe, dict) and recipe:
            recipes[digest(recipe)] = recipe
            requested[str(recipe.get("requested_model") or "unknown")] += 1
        else:
            legacy += 1
    mixed = len(models) > 1 or len(recipes) > 1 or (bool(recipes) and legacy > 0)
    warnings = []
    if len(models) > 1:
        warnings.append("本批结果混用了多个实际模型版本；统一模型重新分析后再比较。")
    if len(recipes) > 1:
        warnings.append("本批结果的分析条件不同；统一条件重新分析后再比较。")
    if recipes and legacy:
        warnings.append("本批混入缺少完整分析条件的旧结果；统一条件重新分析后再比较。")
    if unknown or unversioned:
        warnings.append("部分结果缺少可确认的实际模型版本，不能确认跨期可比性。")
    return {
        "version": "analysis-provenance-v1",
        "actual_models": dict(sorted(models.items())),
        "requested_models": dict(sorted(requested.items())),
        "unknown_model_count": unknown,
        "unversioned_model_count": unversioned,
        "legacy_result_count": legacy,
        "recipes": [recipes[k] for k in sorted(recipes)],
        "mixed": mixed,
        "comparable": bool(models) and not mixed and not unknown and not unversioned,
        "warnings": warnings,
    }


def model_label(info: dict) -> str:
    return "、".join(f"{m}（{n} 条）" for m, n in info.get("actual_models", {}).items()) or "未知"


def schema_label(info: dict) -> str:
    versions = {r.get("schema_version") for r in info.get("recipes", []) if r.get("schema_version")}
    if info.get("legacy_result_count") or not versions:
        return "unknown"
    return next(iter(versions)) if len(versions) == 1 else "mixed"
