# Plan: a general builder for Freya phone agents

## Goal

`python builder/build.py <brief file(s) or folder> --slot <name> [--api-base URL]` reads a bank's brief, builds a Freya workflow, tests it on simulated customers, fixes the failures and exports the result. It has to do this in under 30 minutes, with no manual step and nothing case-specific in its code. It runs on the Claude Agent SDK, which logs in through the Claude Code CLI login (your subscription, so no `ANTHROPIC_API_KEY`).

The inputs come in two forms, and the builder has to handle both:
- **Clean spec** (`briefs/brief-a.md` + `tools/openapi.json`): numbered rules and an OpenAPI tool file.
- **Raw bank material** (`Build the Builder - Final Case/`): emails, an analysis doc that went through several revisions, a meeting transcript, a PDF flow diagram, a prose service contract, a 700 KB test-data JSON. The sources contradict each other, the agent speaks Turkish, and there is no ledger or scorer.

## Core idea: a fixed pipeline with Claude at each step

A Python orchestrator owns the phases, the time budget, the tests and the trace. Claude does the reasoning inside each phase. This is more predictable than one long agent session, and it gives a clean trace.

```
ingest → spec → test-suite → build → lint+dry-run → smoke chat → simulate → judge → fix ─┐
                                        ^                                                │
                                        └──────────── loop until pass or budget ──────────┘
                                                                         → export + trace
```

### Phase 0: Ingest (code, no LLM, about 10 s)
- Walk the input paths. `.md` and `.txt` go in as they are. PDFs go through `pdftotext -layout` (installed) or `pypdf`. Large JSON is reduced to a schema plus 2 samples, and the full file stays available on disk.
- Record each file's date (from the filename or header) and type (contract, analysis, email, meeting, FAQ, test data, examples). Write `work/<slot>/corpus/` plus an `index.md`.

### Phase 1: Spec extraction (Claude, "analyst", about 4 min)
Produces `work/<slot>/spec.json` + `spec.md`, the single source of truth for every later phase:
- **Call variables** (names, formats, examples) and **language/locale**.
- **Tool catalogue** normalised from OpenAPI *or* prose contract: name, path, method, request body shape (including envelopes like `{data:…, call:…}`), response fields, error codes and what each means for the call.
- **Rules**, numbered, each with a source citation. For **conflicts**, record the resolution and why. Precedence: latest decision mail or recap > latest analysis revision > meeting > older docs > FAQ.
- **Verbatim phrases** the bank requires (for example the Turkish sentences quoted in the analysis).
- **Deterministic logic** that belongs in code, not prompts: date math, amount and currency formatting for speech, card selection, which options to offer.
- **End states** (outcomes, transfers, back to menu) and how each one is recorded or signalled.
- **Open questions** with the assumption the builder chose.

### Phase 2: Test suite (Claude, "examiner", about 2 min, runs in parallel with Phase 3)
- Use the given tests where they exist (`public-test-set.json` with its ledger scorer, `example-scenarios.md`).
- Generate 15 to 25 more personas from the spec: happy, edge and adversarial cases, plus the 5 speech styles (pieces, corrects, talkative, asks_back, in_words), in the call's language. Each one gets call variables taken from real test data, a script, and checkable success criteria.

### Phase 3: Build (Claude, "engineer", about 4 min)
Writes `out/<slot>.workflow.json`, following a **patterns skill** distilled from `docs/`, which holds the platform knowledge and nothing about any case:
- An initial **code node** computes every date and formatting fact from `now` into variables.
- **Read-only calls** go in `api_request` passthrough nodes. **Write calls** are tools with `maxRetries: 0`.
- Each tool gets a **post-hook** that turns the raw result into a ready `what_to_do_next` / `say_this` sentence (for example the full Turkish debt reading built in Python). The model reads facts out; it does not compute them.
- **Routing** is on tool-result variables and extracted enums, not on `matches_intent`. Booleans are tested as both `true` and `True`.
- There is a separate `end_call` node per outcome, and global nodes for interrupts that can happen anywhere (human/agent request, off-topic, security probes).
- Tool URLs come from `--api-base` (your Cloudflare mock for the final), so no URL is hard-coded.

### Phase 4: Lint + validate (code, about 5 s)
`builder/lint.py` checks the whole docs checklist, so mistakes the platform drops silently get caught here:
- `default` is used, never `const`. No `null` anywhere. `tools` and `pre_actions` sit on the right node types. `response_mapping` sits at the right level and its paths start with `data.`.
- Every passthrough node ends with `always`. No `{{` in code snippets. Every `{{var}}` is set before it is used. `min_confidence` is set. Every path reaches an `end_call`.
- Every tool name, field and enum value matches the catalogue.

Then a `dryRun` validation. Errors go back to the engineer and are fixed until the lint passes.

### Phase 5: Smoke chat (code, about 30 s)
Use `POST /chat/message` for 3 or 4 turns with the first test persona's variables. This catches "the workflow cannot start", silent turns after tool calls and wrong languages before spending a 3 to 11 minute simulation.

### Phase 6: Simulate + judge (code + Claude, about 6 to 10 min per round)
- `builder/harness.py` generalises `run_cases.py`. It creates personas (cached by hash), runs **all cases in one simulation** with a higher `maxConcurrentCalls`, then pulls each transcript, including the `function_call_*` and `route_evaluated` events.
- **Three signals per case:**
  1. The ledger, where one exists (practice mock). Otherwise the tool calls taken from the transcript events.
  2. Freya's judge verdict.
  3. A Claude **judge** that checks the transcript against the case's checks *and* the spec rules, and gives a root-cause class for each failure: spec misread / prompt / routing / tool schema / hook bug / platform flake (slow turn, hang-up).

### Phase 7: Fix (Claude, "fixer", about 3 min per round)
- The fixer gets the failure clusters (not raw dumps), the relevant transcript slices and the workflow. Its first choice is to move the failing rule *out of prompts and into code or routing*.
- **Regression guard**: keep the best-scoring version. A round that scores lower is reverted.
- Once all cases pass, rerun once to look for flakiness, since the final counts a customer only when 3 out of 3 runs pass.

### Budget (hard 30-minute limit)
The orchestrator tracks the clock. It does not start a simulation round unless at least 12 minutes are left, and it always leaves 2 minutes to save, publish, export and write the trace. That realistically allows **2 to 3 fix rounds**.

## Trace (required)
`traces/<slot>-<time>.log` (plain text) + `.jsonl`. It contains: start and end time; every model call with model and a one-line purpose (the phase name); every Freya object created or updated (workflow id, version, persona ids); every simulation with its scores.

## Mock (final case: a separate component, built before the builder runs)
`mock/worker.js` is a Cloudflare Worker. It loads `test-customers.json` as its table and serves the contract's 7 paths by the lookup keys in `test-customers.md`, with the exact HTTP status and body, plus `delaySeconds` and a `/health` endpoint. Deploy with `npx wrangler deploy` (this needs your Cloudflare login once).

## Generality guardrails
- The builder's code and skills contain only **platform** knowledge (Freya patterns) and **process**. No rule, value, name or language from any brief.
- Check: run the same builder on Brief A, Brief B and the final folder, unchanged.

## File layout
```
builder/build.py        orchestrator, CLI, budget, trace
builder/ingest.py       corpus from files/folders/PDFs
builder/lint.py         static workflow checks
builder/harness.py      personas, simulation, transcripts, tool-call ledger
builder/agents.py       analyst / examiner / engineer / fixer / judge prompts (SDK)
.claude/skills/freya-workflow-patterns/SKILL.md   distilled platform patterns
mock/worker.js, mock/wrangler.toml
```

## Order of work (deadline today 20:00 UTC)
1. `pip install claude-agent-sdk`, then the mock: write, deploy and curl-check it (about 20 min, can run in parallel).
2. Builder MVP: ingest → spec → build → lint/dry-run → one sim round → fix → export, with the trace (about 50 min).
3. Shakedown on Brief A (about 15 min), then fix builder bugs.
4. Final run on the case folder (≤ 30 min), export, `submission.json`, email.
5. Stretch goals after the deadline: the examiner's generated personas, the Claude judge, the style variants, multi-rep flake checks.
