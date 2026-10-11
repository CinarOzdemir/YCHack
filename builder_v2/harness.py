"""Run test customers (from a cases file the builder wrote) against a saved workflow, and collect what happened.

    python builder_v2/harness.py <slot> <cases.json> [--only id1,id2] [--out results.json] [--reps 1]

cases.json:
    {"language": "Turkish",
     "cases": [{"id": "C01", "title": "...", "type": "happy|edge|adversarial",
                "variables": {...call variables...},
                "persona": "who the caller is and what they do, step by step",
                "checks": ["one observable thing the agent must do or not do", ...]}]}

For every case it prints PASS/FAIL with the judge's reason, and writes a JSON file with the compact transcript
(spoken lines, tool calls with arguments and results, node changes, errors) that the fixer reads.
"""
import argparse, hashlib, json, os, pathlib, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "builder_v2"))
os.chdir(ROOT)
import envload  # noqa: E402
import freya
from ops import log, state

PERSONA = """OUTPUT RULE: Write only the words you say on the phone, in {language}. No stage directions, no brackets, no translations. One or two short sentences per turn, then wait for the other side.

WHO YOU ARE AND WHAT HAPPENS IN THIS CALL:
{persona}

RULES:
- Answer what you are asked. Do not volunteer information nobody asked for, unless the text above says you do.
- Do not invent facts that are not written above. If asked something that is not covered, give a short neutral answer.
- When the other side says goodbye or hands you over / ends the conversation, say a short goodbye and say nothing more."""


def compact(call):
    """Transcript lines plus the engine events that matter for debugging."""
    out = []
    for m in call.get("messages", []):
        r = m.get("role")
        if r in ("assistant", "user", "bot"):
            out.append(f"{'AGENT' if r != 'user' else 'CALLER'} [{m.get('nodeId') or ''}]: {m.get('message')}")
        elif r == "system":
            try: e = json.loads(m.get("message") or "{}")
            except Exception: continue
            ev = e.get("event")
            if ev == "node_enter": out.append(f"  -> node {e.get('node')} (from {e.get('from_node')})")
            elif ev == "function_call_start":
                a = e.get("args") or {}
                a = a.get("body", a) if isinstance(a, dict) else a
                out.append(f"  TOOL {e.get('function')} args={json.dumps(a, ensure_ascii=False)[:500]}")
            elif ev == "function_call_complete":
                out.append(f"  RESULT {e.get('function')}: {json.dumps(e.get('result'), ensure_ascii=False)[:900]}")
            elif ev == "error":
                out.append(f"  ERROR {e.get('message')} {json.dumps(e.get('details'), ensure_ascii=False)[:300]}")
    return out


def run(slot, cases_path, only=None, out=None, reps=1):
    spec = json.load(open(cases_path, encoding="utf-8"))
    lang = spec.get("language", "English")
    cases = [c for c in spec["cases"] if not only or c["id"] in only]
    agent_id = state(slot)["agent_id"]
    store = ROOT / ".personas.json"; ids = json.load(open(store)) if store.exists() else {}
    personas = {c["id"]: {"name": f"{slot} {c['id']} {c.get('title', '')}"[:120],
                          "prompt": PERSONA.format(language=lang, persona=c["persona"]),
                          "successCriteria": "All of these are true:\n- " + "\n- ".join(c["checks"]),
                          "tags": ["builder_v2", slot]} for c in cases}
    key = {i: f"{slot}:{i}:" + hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10] for i, p in personas.items()}
    new = [c["id"] for c in cases if key[c["id"]] not in ids]
    if new:
        made = freya.create_personas([personas[i] for i in new])["personas"]
        ids.update({key[i]: p["id"] for i, p in zip(new, made)}); json.dump(ids, open(store, "w"), indent=1)
        log(f"created {len(new)} personas for slot={slot}")
    pid = {c["id"]: ids[key[c["id"]]] for c in cases}
    os.environ.setdefault("FREYA_SIM_CONCURRENCY", str(min(len(cases), 12)))
    concurrency = int(os.environ["FREYA_SIM_CONCURRENCY"])
    all_rows = []
    for rep in range(reps):
        t0 = time.time()
        sim_id, it_id, status = freya.run_simulation(agent_id, f"{slot} {time.strftime('%H:%M:%S')}", {pid[c["id"]]: c["variables"] for c in cases}, concurrency=concurrency)
        took = time.time() - t0
        log(f"simulation slot={slot} sim_id={sim_id} iteration={it_id} status={status} cases={len(cases)} seconds={took:.0f}")
        runs = {r["personaId"]: r for r in freya.get_iteration(sim_id, it_id)["runs"]}
        rows = []
        for c in cases:
            r = runs.get(pid[c["id"]], {})
            row = {"id": c["id"], "title": c.get("title"), "type": c.get("type"), "pass": r.get("outcome") == "pass",
                   "judge": " ".join(str(r.get("reason") or "no verdict").split()), "call_id": r.get("callId"), "checks": c["checks"]}
            if row["call_id"]:
                try:
                    call = freya.get_call(row["call_id"])
                    row["ended"] = call.get("endedReason"); row["transcript"] = compact(call)
                except Exception as e:
                    row["transcript"] = [f"(could not read call: {e})"]
            rows.append(row)
            print(("PASS" if row["pass"] else "FAIL"), f"rep{rep + 1}", c["id"], (c.get("title") or "")[:60], "| ended:", row.get("ended"))
            if not row["pass"]: print("       -", row["judge"][:400])
        n = sum(r["pass"] for r in rows)
        print(f"\nrun {rep + 1}/{reps}: {n} / {len(rows)} passed, simulation {sim_id}, status {status}, {took:.0f} s\n", flush=True)
        all_rows.append(rows)
    final = [{"id": c["id"], "passes": sum(rr[k]["pass"] for rr in all_rows), "runs": reps} for k, c in enumerate(cases)]
    # a customer counts only when EVERY repetition passes (as in the final scoring). For a customer that failed in some
    # repetition the row of a failing repetition is kept, so the fixer reads the transcript of the failure.
    merged = []
    for k, c in enumerate(cases):
        bad = [rr[k] for rr in all_rows if not rr[k]["pass"]]
        row = dict(bad[0] if bad else all_rows[-1][k])
        row["passes"] = f"{final[k]['passes']}/{reps}"; row["pass"] = not bad
        merged.append(row)
    result = {"slot": slot, "agent_id": agent_id, "passed": sum(f["passes"] == reps for f in final), "total": len(final), "final": final, "rows": merged}
    if out:
        pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
        json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("slot"); ap.add_argument("cases"); ap.add_argument("--only"); ap.add_argument("--out"); ap.add_argument("--reps", type=int, default=1)
    a = ap.parse_args()
    r = run(a.slot, a.cases, a.only.split(",") if a.only else None, a.out, a.reps)
    print(f"{r['passed']} / {r['total']} passed")
