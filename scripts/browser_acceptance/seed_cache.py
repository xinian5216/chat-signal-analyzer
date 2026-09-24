# -*- coding: utf-8 -*-
"""把**虚构**聊天的分析结果预置进本地缓存，实现浏览器回归 0 次真实 Jev 调用。

安全属性（硬约束）：

- 只写运行时会读取的那一个 SQLite 缓存文件（``SIGNALLENS_DATA_DIR`` 下的
  ``cache.sqlite3``，或开发模式仓库 ``.jev_cache/cache.db``）；
- **绝不**创建 TypeSafeClient、**绝不**发出任何网络请求；
- 聊天内容全虚构（:mod:`fictional_data`）；
- key 的构造逐字节复刻 ``analyzer.analyze_messages`` 阶段 1：
  ``parse_chat(text, my_name, them_name)`` → ``mask_messages`` →
  ``sort_messages(multi_chunk=False)`` → 前缀累积 → ``build_state`` →
  ``make_cache_key(state, build_questions_schema(), DEFAULT_MODEL,
  SCHEMA_VERSION)``。任何一步与真实运行不一致都会表现为缓存未命中 →
  应用真的去请求 API（会立刻失败，浏览器断言也会抓到外部请求）。

用法::

    .venv\\Scripts\\python -X utf8 scripts\\browser_acceptance\\seed_cache.py \\
        --data-dir <临时数据目录>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 本脚本既可独立运行（``python scripts/browser_acceptance/seed_cache.py``），
# 也被 run_acceptance.py 以模块方式 import；两种情况都保证能找到同目录的
# fictional_data。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from fictional_data import MIXED_TRIGGER_TEXT  # noqa: E402

# seed 之前先固定数据目录：paths 在调用时才读环境变量，但显式先设置可以让
# 本脚本对「以什么身份被 import」不敏感。
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _seed(data_dir: Path) -> dict:
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    os.environ["SIGNALLENS_DATA_DIR"] = str(data_dir)

    import analyzer
    import paths
    import storage
    from fictional_data import ME, TA, build_chat
    from parser import parse_chat
    from privacy import mask_messages
    from timeline import sort_messages

    text = build_chat()
    parsed = parse_chat(text, my_name=ME, them_name=TA)
    messages = sort_messages(mask_messages(parsed),
                             multi_chunk=False).messages

    questions_schema = analyzer.build_questions_schema()
    cache = storage.Cache(paths.cache_db_path())

    prefix: list[dict] = []
    seeded = 0
    targets = 0
    for _i, m in enumerate(messages):
        if (m["speaker"] == "them" and m["text"].strip()
                and m.get("content_type") != "media"):
            targets += 1
            prefix_view = [
                {"speaker": c["speaker"], "text": c["text"],
                 "time": c.get("time")}
                for c in prefix
            ]
            target_state = {"speaker": "them", "text": m["text"],
                            "time": m.get("time")}
            state = analyzer.build_state(prefix_view, target_state)
            key = storage.make_cache_key(
                state, questions_schema, analyzer.DEFAULT_MODEL,
                analyzer.SCHEMA_VERSION)
            cache.set(key, _mock_result(m["text"], targets))
            seeded += 1
        prefix.append({"speaker": m["speaker"], "text": m["text"],
                       "time": m.get("time")})

    return {"seeded": seeded, "targets": targets,
            "db": str(paths.cache_db_path())}


# ---------------------------------------------------------------------------
# 虚构结果（结构 = analyzer.extract_answers 的返回；确定性、无真实判断含义）
# ---------------------------------------------------------------------------

_MODEL = "jev-fixture-mock"


def _score_dict(score: float, confidence: float) -> dict:
    probs = {}
    for level in range(1, 6):
        probs[str(level)] = max(0.01, round(1.0 - abs(level - score), 3))
    total = sum(probs.values())
    probs = {k: round(v / total, 4) for k, v in probs.items()}
    return {"score": round(score, 2), "probabilities": probs,
            "confidence": confidence}


def _choice_dict(choice: str, options: tuple[str, ...],
                 confidence: float) -> dict:
    rest = [o for o in options if o != choice]
    probs = {choice: 0.72}
    weight = 0.28 / max(1, len(rest))
    probs.update({o: round(weight, 4) for o in rest})
    return {"choice": choice, "probabilities": probs,
            "confidence": confidence}


def _noul_value(p: float) -> float:
    return p


def _mock_result(text: str, seq: int) -> dict:
    """确定性的虚构分析结果。

    三类特殊消息保证 UI 回归覆盖面：

    - 含 :data:`MIXED_TRIGGER_TEXT`：关系信息量最高（5）+ 明确疏离概率 →
      同时入选支持性 / 相反证据 → 混合信号标签；
    - ``seq % 17 == 3``：偏温暖的一组（概览指标不为平局）；
    - ``seq % 23 == 5``：偏冷淡的一组（相反证据来源）。
    """
    is_mixed = MIXED_TRIGGER_TEXT in text
    if is_mixed:
        warmth, engagement, special = 2.0, 2.2, 1.8
        evidence = 5.0
        # 0.72 >= NOUL_STRONG_MARK：transform_noul_evidence(0.72)=0.35+ ≥
        # counter_evidence 的 distancing_ev >= 0.30 门槛 → 同一条消息既在
        # 支持性（信息量最高）又在相反证据里 → 混合信号（P0 回归用例）
        romantic, distancing = 0.08, 0.72
        emotion, intent = "annoyed", "distance"
    elif seq % 17 == 3:
        warmth, engagement, special = 3.4, 3.2, 3.0
        evidence = 4.0
        romantic, distancing = 0.18, 0.05
        emotion, intent = "happy", "share_personal"
    elif seq % 23 == 5:
        warmth, engagement, special = 1.2, 1.6, 1.4
        evidence = 2.0
        romantic, distancing = 0.02, 0.05
        emotion, intent = "calm", "perfunctory"
    else:
        warmth, engagement, special = 2.3, 2.4, 1.9
        evidence = 2.0
        romantic, distancing = 0.05, 0.05
        emotion, intent = "calm", "continue_topic"
    conf = round(0.78 + (seq % 5) * 0.03, 2)
    return {
        "emotion": _choice_dict(
            emotion, ("calm", "happy", "teasing", "curious", "caring",
                      "annoyed", "other"), conf),
        "intent": _choice_dict(
            intent, ("ask_information", "explain", "share_opinion",
                     "continue_topic", "show_care", "share_personal",
                     "end_topic", "perfunctory", "distance", "other"), conf),
        "warmth": _score_dict(warmth, conf),
        "engagement": _score_dict(engagement, conf),
        "special_attention": _score_dict(special, conf),
        "relationship_evidence_strength": _score_dict(evidence, conf),
        "relational_ease": _score_dict(2.6, conf),
        "romantic_signal": _noul_value(romantic),
        "distancing_signal": _noul_value(distancing),
        "model": _MODEL,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True,
                    help="SIGNALLENS_DATA_DIR（应用启动时会读同一个目录）")
    args = ap.parse_args()
    info = _seed(Path(args.data_dir))
    print(f"seeded={info['seeded']} targets={info['targets']} "
          f"db={info['db']}", flush=True)
    if info["seeded"] != info["targets"] or info["seeded"] == 0:
        print("SEED MISMATCH", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
