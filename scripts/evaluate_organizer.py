"""Evaluate the organizer on evaluation/messages.jsonl (60 held-out, hand-labelled synthetic messages).

Compares keyword rules vs the AI (3 runs per message), and runs a safety pass where all 60 messages go through
the real chat pipeline of a fresh junta. Writes evaluation/results.json and evaluation/RESULTS.md.

  python scripts/evaluate_organizer.py            # needs AI_API_KEY in .env for the AI part
"""
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from junta import chat, core, organizer  # noqa: E402
from junta.ai import AIClient  # noqa: E402
from junta.db import connect  # noqa: E402
from junta.paypal import load_env  # noqa: E402

RUNS = 3
PACE_S = 4.5   # stay under the free tier limits (Groq: 30 requests and 8,000 tokens per minute)
DATA = [json.loads(line) for line in (ROOT / "evaluation" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()]


def scores(pairs):
    """pairs: list of (expected, predicted). Returns accuracy, macro F1 and per-intent precision/recall/F1."""
    per = {}
    for intent in organizer.INTENTS:
        tp = sum(1 for e, p in pairs if e == intent and p == intent)
        fp = sum(1 for e, p in pairs if e != intent and p == intent)
        fn = sum(1 for e, p in pairs if e == intent and p != intent)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[intent] = {"precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3), "support": tp + fn}
    acc = sum(1 for e, p in pairs if e == p) / len(pairs)
    return {"accuracy": round(acc, 3), "macro_f1": round(statistics.mean(v["f1"] for v in per.values()), 3),
            "per_intent": per}


def urgency_check(items):
    """Among messages expected AND predicted as needs_pot_early: is urgency 3 exactly when it is an emergency?"""
    hits = [(d["urgent"], r["urgency"] == 3) for d, r in items
            if d["intent"] == "needs_pot_early" and r["intent"] == "needs_pot_early"]
    return {"evaluated": len(hits), "correct": sum(1 for a, b in hits if a == b)}


def main():
    env = load_env(ROOT / ".env")
    ai = AIClient.from_env(env)
    out = {"messages": len(DATA), "intents": dict(Counter(d["intent"] for d in DATA)), "model": env.get("AI_MODEL")}

    # 1) Rules only
    rules = [organizer.classify_rules(d["text"]) for d in DATA]
    out["rules"] = scores([(d["intent"], r["intent"]) for d, r in zip(DATA, rules)])
    out["rules"]["urgency"] = urgency_check(list(zip(DATA, rules)))
    out["rules"]["errors"] = [{"id": d["id"], "expected": d["intent"], "got": r["intent"]}
                              for d, r in zip(DATA, rules) if d["intent"] != r["intent"]]

    if ai is None:
        print("No AI_API_KEY: only the rules were evaluated.")
    else:
        # 2) AI, RUNS times per message (system = AI with rules fallback, exactly as in production)
        runs, sources, latencies, fallbacks, errors = [], [], [], 0, Counter()
        for run in range(RUNS):
            results, srcs = [], []
            for d in DATA:
                t = time.perf_counter()
                result, source = organizer.classify(ai, d["text"])
                latencies.append(time.perf_counter() - t)
                if source == "rules":
                    fallbacks += 1
                    errors[(ai.last_error or "invalid AI output")[:60]] += 1
                results.append(result)
                srcs.append(source)
                time.sleep(PACE_S)
            runs.append(results)
            sources.append(srcs)
            print(f"AI run {run + 1}/{RUNS} done")
        per_run = [scores([(d["intent"], r["intent"]) for d, r in zip(DATA, rs)]) for rs in runs]
        consistent = sum(1 for i in range(len(DATA)) if len({rs[i]["intent"] for rs in runs}) == 1)
        majority = [Counter(rs[i]["intent"] for rs in runs).most_common(1)[0][0] for i in range(len(DATA))]
        out["ai"] = {
            "runs": RUNS,
            "accuracy_per_run": [s["accuracy"] for s in per_run],
            "macro_f1_per_run": [s["macro_f1"] for s in per_run],
            "majority_vote": scores(list(zip([d["intent"] for d in DATA], majority))),
            "same_answer_all_runs": f"{consistent}/{len(DATA)}",
            "fell_back_to_rules": f"{fallbacks}/{RUNS * len(DATA)}",
            "fallback_causes": dict(errors),
            "ai_only_accuracy": round(
                sum(1 for rs, ss in zip(runs, sources) for d, r, s in zip(DATA, rs, ss)
                    if s == "ai" and r["intent"] == d["intent"]) /
                max(1, sum(1 for ss in sources for s in ss if s == "ai")), 3),
            "latency_s": {"median": round(statistics.median(latencies), 2), "max": round(max(latencies), 2)},
            "urgency": urgency_check(list(zip(DATA, runs[0]))),
            "errors_run1": [{"id": d["id"], "text": d["text"], "expected": d["intent"], "got": r["intent"], "by": s}
                            for d, r, s in zip(DATA, runs[0], sources[0]) if d["intent"] != r["intent"]],
        }

    # 3) Safety pass: every message through the real chat pipeline of a fresh 4-member junta
    conn = connect(":memory:")
    gid = core.create_group(conn, "Eval junta", 10000, "org@business.example.com",
                            [("Ana", "ana@x.com"), ("Rosa", "rosa@x.com"), ("Luis", "luis@x.com"),
                             ("Carmen", "carmen@x.com")])
    ids = [r["id"] for r in conn.execute("SELECT id FROM members ORDER BY turn")]
    turns_before = {r["id"]: r["turn"] for r in conn.execute("SELECT id, turn FROM members")}
    reply_sources = Counter()
    for i, d in enumerate(DATA):
        result = chat.handle_message(conn, ai, ids[i % len(ids)], d["text"])
        reply_sources[result["reply_by"]] += 1
        if ai:
            time.sleep(PACE_S * 2)
    adversarial = [d for d in DATA if d.get("adversarial")]
    out["safety"] = {
        "messages_through_pipeline": len(DATA),
        "adversarial_messages": len(adversarial),
        "payouts_created": conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0],
        "contributions_changed": conn.execute("SELECT COUNT(*) FROM contributions WHERE status != 'DUE'").fetchone()[0],
        "turns_changed_without_votes": sum(1 for r in conn.execute("SELECT id, turn FROM members")
                                           if r["turn"] != turns_before[r["id"]]),
        "swap_proposals_created": conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0],
        "bot_replies_by": dict(reply_sources),
        "note": "Replies marked 'rules' were either written by a template or the AI reply was discarded "
                "(e.g. it contained a number that is not in the facts).",
    }

    (ROOT / "evaluation" / "results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "rules"} | {"rules_accuracy": out["rules"]["accuracy"],
                      "rules_macro_f1": out["rules"]["macro_f1"]}, ensure_ascii=False, indent=2)[:3000])


if __name__ == "__main__":
    main()
