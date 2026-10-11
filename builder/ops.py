"""Freya operations for the builder. Run from the kit root.

    python builder/ops.py save <slot> <workflow.json>                lint, dry-run, save + publish. Prints ids.
    python builder/ops.py chat <slot> <variables.json|'{...}'> "msg 1" "msg 2" ...
                                                                     talk to the published workflow turn by turn
    python builder/ops.py export <slot> <out.json>                   export the workflow as it is on the platform

Every Freya object touched is appended to work/freya-actions.log (part of the trace).
"""
import json, os, pathlib, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "builder"))
os.chdir(ROOT)
import envload  # noqa: E402  (reads env.sh when the variables are not set)
import freya
from lint import lint

LOG = ROOT / "work" / "freya-actions.log"


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f: f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg + "\n")


def load(path):
    w = json.load(open(path, encoding="utf-8"))
    if "workflow" in w and "nodes" not in w: w = w["workflow"]
    w.pop("agent_config", None)
    return w


def save(slot, path):
    w = load(path)
    err, warn = lint(w)
    for x in warn: print("WARNING", x)
    if err:
        for e in err: print("ERROR  ", e)
        print("NOT SAVED: fix the lint errors first."); return 1
    try:
        freya.validate_workflow(slot + " dry-run", w)
    except RuntimeError as e:
        print("NOT SAVED: platform validation failed:", str(e)[:3000]); return 1
    r = freya.save_workflow(slot, w)
    log(f"saved workflow slot={slot} workflow_id={r['workflow_id']} agent_id={r['agent_id']} version={r.get('version', 1)} from {path}")
    print(json.dumps(r))
    return 0


def state(slot):
    s = json.load(open(ROOT / ".freya-agents.json"))
    if slot not in s: sys.exit(f"slot {slot} was never saved")
    return s[slot]


def chat(slot, variables, messages):
    wid = state(slot)["workflow_id"]
    v = json.loads(open(variables, encoding="utf-8").read()) if os.path.exists(variables) else json.loads(variables)
    call_id = None
    for m in messages:
        body = {"workflowId": wid, "message": m, "customerVariables": v}
        if call_id: body["callId"] = call_id
        t = time.time()
        try:
            r = freya.api("POST", "/api/v2/chat/message", body)
        except RuntimeError as e:
            print(f"USER: {m}\nERROR: {str(e)[:800]}"); break
        call_id = r.get("callId") or call_id
        print(f"USER: {m}\nAGENT ({time.time() - t:.1f}s): {r.get('response')}\n")
    if call_id:
        try:
            freya.api("POST", "/api/v2/chat/end", {"callId": call_id})
            call = freya.get_call(call_id)
            print("--- events ---")
            for msg in call.get("messages", []):
                if msg.get("role") == "system":
                    try: e = json.loads(msg["message"])
                    except Exception: continue
                    ev = e.get("event")
                    if ev in ("function_call_start", "function_call_complete", "error", "node_enter"):
                        print(ev, json.dumps({k: e.get(k) for k in ("node", "function", "args", "result", "message", "details") if e.get(k) is not None}, ensure_ascii=False)[:700])
        except Exception as e:
            print("(could not read events:", str(e)[:200], ")")
    log(f"chat test slot={slot} call_id={call_id} turns={len(messages)}")


def export(slot, out):
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    freya.export_workflow(state(slot)["workflow_id"], out)
    log(f"exported slot={slot} to {out}")
    print(out)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    if a[0] == "save" and len(a) == 3: sys.exit(save(a[1], a[2]))
    if a[0] == "chat" and len(a) >= 4: chat(a[1], a[2], a[3:]); sys.exit(0)
    if a[0] == "export" and len(a) == 3: export(a[1], a[2]); sys.exit(0)
    sys.exit(__doc__)
