"""真实 API 冒烟测试（本机手动运行，不在 pytest / CI 中执行）。

v2：只用 5 条构造的 TA 消息（共 5 次 Jev 请求），验证：
- relationship_evidence_strength 能由 Jev 正常返回；
- 功能性短句（“哦哦”“知道了”）evidence 应较低；
- 明显关系表达 / 疏离表达（“你比较重要”“以后别找我了”）evidence 应明显较高。

第二次运行应全部命中本地缓存（0 次额外请求）。

用法（项目根目录）：
    .venv\\Scripts\\python scripts\\smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from analyzer import DEFAULT_MODEL, analyze_messages, create_client
from parser import parse_chat
from privacy import mask_messages
from scoring import (
    compute_conversation_stats,
    is_low_evidence_display,
    message_metrics,
    relational_ease_label,
)
from storage import Cache

# v2.1：验证新增 relational_ease（互动熟悉度）能否由 Jev 正常返回。
# 5 条构造样本覆盖“正式事务 / 自然调侃 / 接梗 / 纯事务 / 默契”五类熟悉度。
SAMPLE_CHAT = """我: 在忙吗
TA: 收到，谢谢
我: 周末出去玩吗
TA: 哈哈你又来了
我: 你还记得那个梗啊
TA: 你还记得那个梗啊哈哈哈
我: 请看一下附件
TA: 请确认附件是否收到
我: 这事只有你懂
TA: 行行行，还是你懂我"""

EXPECT_EASE = {
    "收到，谢谢": "低~中",
    "哈哈你又来了": "中~高",
    "你还记得那个梗啊哈哈哈": "高",
    "请确认附件是否收到": "低",
    "行行行，还是你懂我": "高",
}


def main() -> None:
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        print("未设置 TYPESAFE_API_KEY，请在 .env 中配置后再运行。")
        sys.exit(1)

    messages = mask_messages(parse_chat(SAMPLE_CHAT))
    cache = Cache()
    client = create_client(api_key=api_key)

    results = analyze_messages(client, messages, cache=cache)
    stats = compute_conversation_stats(results)

    print("=== 逐条结果（关注关系信息量）===")
    for e in results:
        if e.get("error"):
            print(f"[{e['index']}] 失败: {e['error']}")
            continue
        r = e["result"]
        m = message_metrics(e)
        print(
            f"[{e['index']}] TA: {e['text']}\n"
            f"    情绪: {r['emotion']['choice']}  意图: {r['intent']['choice']}\n"
            f"    warmth={r['warmth']['score']:.2f} engagement={r['engagement']['score']:.2f} "
            f"special={r['special_attention']['score']:.2f}\n"
            f"    关系信息量(evidence)={r['relationship_evidence_strength']['score']:.2f}/4 "
            f"(evidence_conf={r['relationship_evidence_strength']['confidence']:.2f}) "
            f"weight={m['weight']:.3f}\n"
            f"    互动熟悉度(ease)={r['relational_ease']['score']:.2f}/4 "
            f"(ease_conf={r['relational_ease']['confidence']:.2f})\n"
            f"    romantic={r['romantic_signal']:.2f} distancing={r['distancing_signal']:.2f} "
            f"base_score={m['base_score'] * 100:.0f}"
        )

    print("\n=== 聚合（v2 加权 + v2.1 解释层）===")
    keys = (
        "overall", "overall_sufficient", "recent", "trend",
        "effective_messages", "analyzed", "total_weight",
        "romantic_evidence", "distancing_evidence",
        "warmth_avg", "engagement_avg", "special_attention_avg",
        "relational_ease_avg",
    )
    print(json.dumps({k: stats[k] for k in keys}, ensure_ascii=False, indent=2))
    print("低信息量展示模式：", is_low_evidence_display(stats))
    print("互动熟悉度标签：", relational_ease_label(stats["relational_ease_avg"]))

    # 验证 relational_ease 区分度：正式事务 / 纯事务应低，调侃 / 接梗 / 默契应高
    ease = {e["text"]: e["result"]["relational_ease"]["score"]
            for e in results if e.get("result")}
    low_texts = ("收到，谢谢", "请确认附件是否收到")
    high_texts = ("哈哈你又来了", "你还记得那个梗啊哈哈哈", "行行行，还是你懂我")
    low = [ease.get(t) for t in low_texts]
    high = [ease.get(t) for t in high_texts]
    print("\n=== relational_ease 合理性检查 ===")
    print(f"低熟悉度样本(正式/事务): {dict(zip(low_texts, low))}")
    print(f"高熟悉度样本(调侃/接梗/默契): {dict(zip(high_texts, high))}")
    if (all(v is not None for v in low) and all(v is not None for v in high)
            and max(low) < min(high)):
        print("✅ 符合预期：正式/事务性 ease 低，调侃/接梗/默契 ease 明显较高")
    else:
        print("⚠️ 与预期不符，请把原始输出反馈给开发，不要自行反复调 prompt")

    # 第二次运行应全部命中缓存
    client2 = create_client(api_key=api_key)
    again = analyze_messages(client2, messages, cache=cache)
    cached_hits = sum(1 for e in again if e.get("cached"))
    print(f"\n缓存命中：{cached_hits}/{len(again)}（应为 {len(again)}，即 0 次额外 API 请求）")
    cache.close()


if __name__ == "__main__":
    main()
