"""The builder: brief in, tested Freya phone agent out.

    python builder/build.py <brief file or folder> [more files/folders] --slot <name> [--api-base URL] [--budget-min 29]

Phases (each Claude phase is one Claude Agent SDK session; the Python around them owns time, tests and trace):
    1. ingest          code     every input file as text (PDFs converted, big JSON summarised)
    2. spec + tests    Claude   analyst writes spec.md, examiner writes cases.json (in parallel)
    3. build           Claude   engineer writes the workflow, saves it (lint + dry run + publish), chats with it
    4. test / fix      code + Claude   simulate all test customers, fixer repairs failures; best version kept
    5. export          code     the published workflow to submission/<slot>.workflow.json, trace to traces/

Nothing in this file or in the skill is about a particular bank or brief. Everything case-specific is read from the
inputs at run time. Uses the Claude login of this machine (subscription) unless ANTHROPIC_API_KEY is set.
"""
import argparse, asyncio, dataclasses, json, os, pathlib, shutil, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "builder"))
os.chdir(ROOT)
import envload  # noqa: E402
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

MODEL = os.environ.get("BUILDER_MODEL", "claude-opus-5-5")
PY = sys.executable.replace("\\", "/")


class Trace:
    def __init__(self, slot):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (ROOT / "traces").mkdir(exist_ok=True)
        self.txt = (ROOT / "traces" / f"{slot}-{stamp}.log").open("w", encoding="utf-8")
        self.jsonl = (ROOT / "traces" / f"{slot}-{stamp}.jsonl").open("w", encoding="utf-8")
        self.path = self.txt.name
        self.t0 = time.time()

    def line(self, msg):
        s = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [+{(time.time() - self.t0) / 60:5.1f} min] {msg}"
        print(s, flush=True); self.txt.write(s + "\n"); self.txt.flush()

    def raw(self, phase, obj):
        self.jsonl.write(json.dumps({"phase": phase, "t": round(time.time() - self.t0, 1), **obj}, default=str, ensure_ascii=False) + "\n"); self.jsonl.flush()


async def claude(trace, phase, purpose, prompt, timeout_s):
    """One Claude session. Logs every model turn (model + one-line purpose) and every tool it uses."""
    opts = ClaudeAgentOptions(
        cwd=str(ROOT), model=MODEL, skills=["freya-builder"], setting_sources=["project"],
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"], permission_mode="dontAsk", max_turns=200,
        env={"BASH_DEFAULT_TIMEOUT_MS": "900000", "BASH_MAX_TIMEOUT_MS": "1200000", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
    )
    trace.line(f"MODEL CALL START phase={phase} model={MODEL} purpose: {purpose} (time limit {timeout_s / 60:.0f} min)")
    turns = 0

    async def go():
        nonlocal turns
        async for m in query(prompt=prompt, options=opts):
            trace.raw(phase, {"type": type(m).__name__, **(dataclasses.asdict(m) if dataclasses.is_dataclass(m) else {"repr": repr(m)})})
            if isinstance(m, AssistantMessage):
                turns += 1
                for b in m.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        trace.line(f"  [{phase}] model={getattr(m, 'model', MODEL)} says: {' '.join(b.text.split())[:300]}")
                    elif isinstance(b, ToolUseBlock):
                        arg = b.input.get("command") or b.input.get("file_path") or b.input.get("pattern") or ""
                        trace.line(f"  [{phase}] model={getattr(m, 'model', MODEL)} tool {b.name}: {str(arg)[:200]}")
            elif isinstance(m, ResultMessage):
                trace.line(f"MODEL CALL END phase={phase}: {m.num_turns} turns, {m.duration_ms / 1000:.0f} s, cost ${m.total_cost_usd or 0:.2f}")
    try:
        await asyncio.wait_for(go(), timeout_s)
    except asyncio.TimeoutError:
        trace.line(f"MODEL CALL STOPPED phase={phase}: time limit reached after {turns} turns")
    except Exception as e:
        trace.line(f"MODEL CALL ERROR phase={phase}: {e!r}")


def sh(trace, args, label, timeout=1500):
    trace.line(f"RUN {label}: {' '.join(args)}")
    p = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    for l in out.strip().splitlines()[-60:]: trace.line(f"  | {l}")
    return p.returncode, out


COMMON = """You are part of an automated builder that turns a bank's brief into a Freya phone agent (a Freya workflow).
Work from the folder {root}. Use `{py}` for Python. Paths with spaces must be quoted.
The skill `freya-builder` (.claude/skills/freya-builder/SKILL.md) holds the platform knowledge: read it first.
Everything about the case comes from the inputs. Do not assume any rule, value or phrase that the inputs do not give.
No questions to anyone: there is no human in the loop. Decide, note your decision, and continue.
You have about {minutes} minutes for this phase. Work fast and finish in time; an unfinished phase is worse than a good-enough one.
"""

SPEC = COMMON + """
PHASE: SPEC. You are the analyst.

Inputs: every file of the brief, as text, is listed in {work}/corpus_index.md (folder {work}/corpus/). Read ALL of them.
{api_note}

Write {work}/spec.md: the single, complete, conflict-free specification the engineer builds from. It must contain:
1. Job of the call, language of the conversation, tone, who calls whom.
2. Call variables: exact names, formats, examples (only those the inputs say the agent receives).
3. Tool catalogue: for every service/tool the agent may call: full URL, method, headers, the EXACT request body JSON (with which
   fields come from which variable or earlier result), response fields that matter with their paths, every error/status case and
   what the agent must do in each. Note slow responses if the inputs mention them.
4. The conversation flow step by step, as numbered rules. Every rule a test could check: what to say (EXACT phrases, verbatim, in the
   call's language, with placeholders), in which order, what never to say or reveal, how many tries, confirmations, how to treat vague
   answers, interruptions and off-topic requests, pressure and security cases, every way the call can end and what the agent says then.
5. Formatting rules for speech: numbers, amounts, currencies, dates (in the call's language), with worked examples taken from the inputs.
6. Conflicts between sources: list each one and the resolution. The newer, more specific decision wins (a decision recap or the latest
   revision beats older drafts, meetings beat older documents, FAQs are least authoritative), unless a newer source says otherwise.
7. Open points with the assumption you chose.
8. Worked examples: for 3 to 5 test records from the inputs, what the agent should say, computed from the real data.
Be exhaustive on rules and concise in prose. Do not design the workflow.
"""

CASES = COMMON + """
PHASE: TESTS. You are the examiner.

Inputs: every file of the brief, as text, is listed in {work}/corpus_index.md (folder {work}/corpus/). Read the brief, the example
scenarios or test cases if there are any, and the test data (customers, accounts, service answers).

Write {work}/cases.json, the simulated customers for testing the agent, in this format:
{{"language": "<language the callers speak>",
  "cases": [{{"id": "C01", "title": "short", "type": "happy|edge|adversarial",
             "variables": {{<every call variable the agent receives, with real values from the test data so the tools return real answers>}},
             "persona": "Who the caller is and exactly what they do and say, in order, with the facts they know. Written in English; the caller speaks the language above.",
             "checks": ["one observable thing the agent must or must not do, with the exact values expected from the test data", "..."]}}]}}
Rules:
- 14 to 16 cases. First, one case per example scenario given in the inputs (same variables and behaviour). Then cover the remaining
  branches of the brief: error answers of each service, empty or odd data, every way the call can end, pressure, security probes,
  callers who change their mind, give vague answers, say things in pieces or in words.
- Use the real test records: pick the record whose data produces the branch you test. Check the expected values in the test data.
- Use the same date/time format for time variables as the inputs show.
- 3 to 6 checks per case, each judgeable from the transcript alone. Keep them precise and fair: only what the brief requires.
Validate the JSON with `{py} -c "import json;json.load(open('{work}/cases.json',encoding='utf-8'))"`.
"""

BUILD = COMMON + """
PHASE: BUILD. You are the engineer.

1. Read .claude/skills/freya-builder/SKILL.md, then docs/workflow-format.md and docs/tools-and-code.md (all of them, they are the
   platform reference), then {work}/spec.md (the case). {cases_note}
2. Write the workflow JSON to {wf}. Follow the skill's shape: code computes, the model reads; writes only after an explicit yes;
   routing on string flags; exact phrases from the spec; one end node per outcome. Cover every rule of the spec.
   Write it in pieces if it is large (Python script that assembles and dumps the JSON is fine: keep that script in {work}/).
3. Save it: `{py} builder/ops.py save {slot} {wf}`. Fix every ERROR and re-save until it prints the workflow_id and agent_id.
   Look at the WARNINGs and fix the real ones.
4. Talk to it: `{py} builder/ops.py chat {slot} '<call variables JSON>' "<first caller line>" "<next>" ...` with the variables of 2 or 3
   different cases from {work}/cases.json (if it exists), 2 to 4 turns each, in the call's language. Check: it starts, it speaks the
   right language, the exact phrases are right, tools are called with correct bodies, results are read correctly, nothing is silent.
   Fix and re-save what is wrong.
Finish with the workflow saved and published. Your last line: workflow_id, agent_id and what you tested.
"""

FIX = COMMON + """
PHASE: FIX (round {round}). You are the fixer.

The workflow is {wf} (saved as slot {slot}). The spec is {work}/spec.md, the test customers {work}/cases.json.
The last simulation passed {passed} of {total}. Results with compact transcripts (spoken lines, TOOL calls with args, RESULTs, node
changes, errors) and the judge's reason for every case: {results}.

1. For every FAILED case, find the root cause in the transcript: spec misread, missing rule, prompt too vague, wrong routing, tool body
   or mapping wrong, hook bug, agent silent after a tool, or a test that is wrong/unfair (the case contradicts the spec), or a slow
   platform/hang-up. Group failures with the same cause.
2. Fix the workflow, preferring code and routing over prompt text (see the skill). Do not break what passes: read the passing
   transcripts of the node you change. If a case itself is wrong, fix it in {work}/cases.json (only when the spec clearly disagrees).
3. Save: `{py} builder/ops.py save {slot} {wf}`. Then replay the start of 1 or 2 failing cases with
   `{py} builder/ops.py chat ...` to confirm the fix.
Earlier rounds' results are in the same folder (round*.json): a case that passed before and fails now points at a flaky rule; make
that rule deterministic (code/routing/exact tts line) rather than adding prompt text.
Finish with the workflow saved. Last line: what you changed, one line per cause.
"""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+"); ap.add_argument("--slot", required=True)
    ap.add_argument("--api-base", default="", help="base URL of the bank's tool API to use in the workflow (replaces the host in the inputs)")
    ap.add_argument("--budget-min", type=float, default=29.0); ap.add_argument("--max-rounds", type=int, default=8)
    a = ap.parse_args()
    t0 = time.time(); deadline = t0 + a.budget_min * 60
    left = lambda: (deadline - time.time()) / 60
    slot = a.slot; work = ROOT / "work" / slot; work.mkdir(parents=True, exist_ok=True)
    wf = f"out/{slot}.workflow.json"; (ROOT / "out").mkdir(exist_ok=True)
    tr = Trace(slot)
    actions = ROOT / "work" / "freya-actions.log"; actions_from = actions.stat().st_size if actions.exists() else 0
    tr.line(f"BUILD START slot={slot} inputs={a.inputs} api_base={a.api_base or '(from inputs)'} builder_model={MODEL} budget={a.budget_min} min")

    import ingest
    rows = ingest.main(str(work), [str(pathlib.Path(p).resolve()) for p in a.inputs])
    tr.line(f"INGEST {len(rows)} files -> {work / 'corpus'}")

    fmt = dict(root=ROOT, py=PY, work=f"work/{slot}", wf=wf, slot=slot)
    api_note = (f"The bank's services are reachable for this agent at the base address {a.api_base} : wherever the inputs give a base "
                f"address or host for the services, the workflow must use {a.api_base} instead, with the same paths after it." if a.api_base else
                "Take the service addresses from the inputs.")
    m = max(4, min(8, left() * 0.25))
    await asyncio.gather(
        claude(tr, "spec", "read all brief sources, resolve conflicts, write spec.md", SPEC.format(**fmt, minutes=int(m), api_note=api_note), m * 60),
        claude(tr, "tests", "write simulated test customers cases.json from the brief and its test data", CASES.format(**fmt, minutes=int(m)), m * 60),
    )
    if not (work / "spec.md").exists(): tr.line("spec.md missing: the engineer reads the corpus directly")
    cases = work / "cases.json"
    try:
        json.load(open(cases, encoding="utf-8")); has_cases = True
    except Exception:
        has_cases = False; tr.line("cases.json missing or invalid: no simulation possible")

    m = max(6, min(12, left() - 12))
    await claude(tr, "build", "write the workflow from spec.md, save it, chat-test it",
                 BUILD.format(**fmt, minutes=int(m), cases_note=f"Test customers: work/{slot}/cases.json." if has_cases else ""), m * 60)
    if not (ROOT / wf).exists(): tr.line("BUILD FAILED: no workflow file"); return
    rc, out = sh(tr, ["builder/ops.py", "save", slot, wf], "save")
    best = None  # (passed, file)
    rnd = 0
    while has_cases and left() > 4.5 and rnd < a.max_rounds:
        rnd += 1
        res_file = f"work/{slot}/round{rnd}.json"
        rc, out = sh(tr, ["builder/harness.py", slot, f"work/{slot}/cases.json", "--out", res_file], f"simulation round {rnd}")
        try: res = json.load(open(ROOT / res_file, encoding="utf-8"))
        except Exception: tr.line("simulation produced no result"); break
        tr.line(f"TEST round {rnd}: {res['passed']} / {res['total']} passed. Failed: {[r['id'] for r in res['rows'] if not r['pass']]}")
        snap = work / f"workflow.round{rnd}.json"; shutil.copy(ROOT / wf, snap)
        if best is None or res["passed"] > best[0]:
            best = (res["passed"], snap)
        elif res["passed"] < best[0]:
            tr.line(f"round {rnd} is worse than the best ({best[0]}): restoring {best[1].name}")
            shutil.copy(best[1], ROOT / wf); sh(tr, ["builder/ops.py", "save", slot, wf], "restore best")
        if res["passed"] == res["total"] or left() < 7: break
        m = max(3, min(7, left() - 5))
        await claude(tr, f"fix{rnd}", f"diagnose {res['total'] - res['passed']} failed test customers and repair the workflow",
                     FIX.format(**fmt, minutes=int(m), round=rnd, passed=res["passed"], total=res["total"], results=res_file), m * 60)
        if left() < 4.5:     # no time to verify this fix with a simulation: ship the best tested version instead
            if best:
                tr.line(f"no time to test the last fix: restoring the best tested version {best[1].name} ({best[0]} passed)")
                shutil.copy(best[1], ROOT / wf)
            break
    rc, out = sh(tr, ["builder/ops.py", "save", slot, wf], "final save")
    if rc != 0 and best:
        tr.line("final save failed: restoring the best tested version"); shutil.copy(best[1], ROOT / wf); sh(tr, ["builder/ops.py", "save", slot, wf], "restore best")
    sub = f"submission/{slot}.workflow.json"
    sh(tr, ["builder/ops.py", "export", slot, sub], "export")
    if actions.exists():
        tr.line("FREYA OBJECTS created or changed in this run (work/freya-actions.log):")
        with actions.open("rb") as f:
            f.seek(actions_from)
            for l in f.read().decode("utf-8", "replace").splitlines(): tr.line("  " + l)
    tr.line(f"BUILD END slot={slot}: {(time.time() - t0) / 60:.1f} min. Best tested score: {best[0] if best else 'n/a'}. Exported {sub}. Trace {tr.path}")


if __name__ == "__main__":
    asyncio.run(main())
