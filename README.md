# CacheBros: a builder for Freya bank phone agents

Give it a bank's brief (a clean spec or raw material such as emails, PDFs, meeting transcripts and service contracts) and it builds, tests and fixes a Freya phone agent (a Freya workflow), with no manual step.

```
python builder_v2/build.py <brief file or folder> --slot <name> --api-base <bank tool API base URL> [--budget-min 29] [--reps 2]
```

It uses the Claude login of the machine running it (Claude Agent SDK), and `FREYA_API_KEY` from `env.sh` (copy `env.example`; never commit it).

## How it works (`builder_v2/`)

| Step | Who | What |
|---|---|---|
| 1. Ingest (`ingest.py`) | code | Every input file as text. PDFs converted, big JSON summarised. |
| 2. Spec | Claude "analyst" | One conflict-free `spec.md`: tools, exact phrases, rules. The newest decision wins. |
| 3. Tests | Claude "examiner" + independent "reviewer" | 16 simulated customers with checks, then a second session removes wrong or unfair checks. |
| 4. Build | Claude "engineer" | Writes the workflow using the patterns in `.claude/skills/freya-builder-v2`, lints it, saves, publishes, chat-tests it. |
| 5. Test and fix | code + Claude "fixer" | Simulates every customer `--reps` times (a customer passes only if all pass). The fixer repairs failures, preferring code and routing over prompts. The best-scoring version is kept. |
| 6. Export | code | `submission/<slot>.workflow.json` and a plain-text trace in `traces/`. |

Helpers: `lint.py` (platform pitfalls Freya drops silently), `ops.py` (save, publish, chat, export), `harness.py` (personas, simulation, compact transcripts).

Nothing case-specific is in the code or the skill. The bank's rules, phrases, tools and language come from the brief at run time.

## Mock bank service (`mock/`)

A Cloudflare Worker that serves the bank's seven statement-debt services from the test customers (`mock/data.json`), with the exact answers and HTTP statuses. Deployed at https://freya-statement-mock.berkin-freya.workers.dev (`GET /health`; services are `POST /voice/v1/<service>`). Deploy with `cd mock && npx wrangler deploy`.

## Final submission

`submission.json`, the exported workflow `submission/final4.workflow.json` and the trace `traces/final4-20261011-221129.log`. Code commit that produced it: `ffae793`. Our own test result: 13/16 on a single pass, 10/16 when every customer must pass twice.

## Layout

```
builder_v2/   the builder
.claude/skills/freya-builder-v2/  Freya platform patterns for the builder
mock/         Cloudflare Worker  docs/  Freya platform docs, plan, kit readme
briefs/ tools/ public-test-set.json freya.py examples/base-agent.json   event kit files
```
