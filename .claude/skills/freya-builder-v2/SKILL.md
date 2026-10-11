---
name: freya-builder-v2
description: Platform knowledge and proven patterns for building a reliable Freya phone-agent workflow from a bank's spec. Use in every phase of the builder (spec, tests, build, fix).
---

# Building a reliable Freya phone agent

The full platform reference is in `docs/workflow-format.md`, `docs/tools-and-code.md` and `docs/api.md`. Read them before you write or change a workflow. This page is the short list of what makes an agent pass **every** time. The phone-agent model is small and fast: it follows short, concrete instructions and fails at arithmetic, date math, formatting, long rule lists and remembering counters. So put every rule you can into **code and routing**, and leave the model only the talking.

Nothing here is about a particular bank. Every rule, phrase, value, tool field and URL comes from the spec of the case you are building.

## The shape that works

```
setup (code, initial, passthrough)          computes date facts from the `now` call variable, sets every flag to a sentinel
  -> lookup_x (api_request, passthrough)    engine-run read calls. post_process_code turns the raw answer into ready facts
  -> decide (code, passthrough)             branches on the facts: writes a string `route` variable
  -> conversation nodes                     one per stage with strict rules. Tools only where the model must decide
  -> do_write (api_request, passthrough)    write actions entered only after an explicit confirmation was extracted
  -> end nodes (end_call)                   one per outcome, distinct terminal function names
global_nodes                                interrupts valid everywhere (exclude end nodes and write nodes)
```

## Rules that matter most

1. **Code computes, the model reads.** Any sentence that depends on data (amounts, dates, lists of options, which items exist, what to offer) is built in Python (a code node or a post-hook) and handed to the model as a ready string variable, e.g. `{{reading_text}}`. The prompt says: "Say this text exactly, word for word: {{reading_text}}". Format numbers, currencies and dates in the spec's language and style in Python. Never ask the model to do date math, rounding or formatting.
2. **Post-hooks give the model an instruction.** On every `api_call` tool add a `post_process_code` that (a) removes `header`, (b) reads the bank's envelope defensively, and (c) writes a plain `what_to_do_next` (and when useful `say_text`) field into `result['data']` in the call's language, covering success, "not found" and technical error. A hook can also write `vars['x'] = ...` for routing. The model must never be left with bare data or silence after a tool result.
3. **Errors in a post-hook.** The shape of `result` on an HTTP error status is not documented. Handle all of: `result` not a dict; `result.get('status')` not `success`; `result.get('status_code')` >= 400; `result['data']` missing or holding the bank's error envelope; the bank's own success flag/code not the success value. Classify into string states like `ok`, `not_found`, `error` and write them into vars and data.
4. **Route on strings set by code or extraction.** Use `equals` on string flags (`ok`, `error`, `yes`, `no`, `week_3`). Booleans from APIs: test both `"true"` and `"True"`. Use `matches_intent` only where no variable can carry the decision, and never to leave a node that must first perform a tool call.
5. **Writes are engine-run after an explicit yes.** For an action with side effects (a change, a send, a promise): the conversation node reads back the details and asks for confirmation; an extraction enum (`yes`, `no`, `unclear`, `min_confidence` about 0.6) captures the answer; only `equals yes` leads to a passthrough `api_request` node whose `pre_actions` performs the write with `maxRetries: 0`; the next node reports success or failure from the result flag. This guarantees "exactly once" and "never claim success when it failed". A vague answer ("I guess") must map to `unclear`: say so in the extraction description.
6. **Exact phrases.** When the spec gives a sentence to say, put it in `on_enter` `tts` text (with `{{var}}`) or in a code-built string, not as a paraphrase request. Fixed farewells go in the end node's `spoken_message`.
7. **Every node's prompt is short and local.** `task_message`: the job of this node, the exact words to use, what not to say, and which tool to call when. No routing talk, no node names. State the language in `system_prompt` ("Speak only Turkish" etc.) plus global rules (identity, honesty, never invent figures, never reveal what the spec forbids).
8. **Model-filled tool fields** need `enum` where values are known, and a description naming the exact source. Values the workflow knows (ids, numbers from earlier results) must be baked with `"default": "{{var}}"` so the model never types them.
9. **Counters and tries live in code or in `max_node_turns` + `max_turn_transition`**, or in `<node>.current_turns`, never in the model's memory.
10. **Turn-taking.** In every conversation prompt: "After a tool result, always say something to the caller in the same turn. One question at a time."
11. **Timeouts.** Read calls: `timeoutMs` 20000, `maxRetries` 1. Writes: `maxRetries` 0. A slow answer is still a valid answer.
12. **Variables a prompt or `tts` line uses must always be set** (the setup node writes sentinels). An unset `{{x}}` is read aloud as `{{x}}`.

## Freya pitfalls (all silent)

- `tools` only on `conversation` nodes, `pre_actions` only on `api_request` / `code` nodes.
- `response_mapping` on the tool for `tools`, on the **node** for `pre_actions`. Paths start with `data.` (the API's JSON body is under `data` in the envelope; a bank body `{"data": {...}}` is therefore at `data.data....`).
- `default`, never `const`. No `null` anywhere. Leave `agent_config` out.
- Each `api_call` body field is its own leaf under `body.properties` with its own `default`. A nested object in the body (e.g. `{"data": {...}, "call": {...}}`) is a leaf of type `object` with properties of its own, each with a `default`; if unsure, build the whole body in a `pre_process_code` hook by setting `config['body']` to a dict.
- Code: never write `{{` or `}}` in code or hooks (split nested dict literals: `d = {}; d['a'] = {'b': 1}`), read everything with `vars.get`, wrap in `try/except`, write a fallback for every variable later read. Modules: `json`, `re`, `datetime`.
- Passthrough nodes end with an `always` transition and have `passthrough_default_transition`.
- `end_call` farewell comes from `terminal.function.parameters.spoken_message` with `message_mode: custom`; `on_enter` silent; `transitions: []`; `disable_inactivity_monitor: true`.
- Extraction: `min_confidence` on every field (0.6 to 0.8), dates as `type: date`, enums via `enum_values`.
- `{{var}}` in a code node's source is not substituted reliably: use `vars`.
- After a save, the version must be published: `python builder_v2/ops.py save` does it.

## Tools you have in the builder

- `python builder_v2/ops.py save <slot> <file>`: lint + platform dry run + save + publish. Fix every ERROR it prints.
- `python builder_v2/ops.py chat <slot> '<json vars>' "msg1" "msg2" ...`: a real conversation in seconds, with the tool events. Use it after each save to see the opening and one tool round trip. Seen on 11 Oct: the chat route does NOT reliably apply the call variables you pass (the agent invented a name). So set the workflow's top-level `test_variables` to one real test record (every call variable), which a chat call falls back to; simulations pass their own variables, which win. If chat output looks wrong only because of variables, do not "fix" it. Also a code node as first step may be skipped by chat: judge code nodes by the simulation.
- `python builder_v2/harness.py <slot> <cases.json> --out <file>`: full simulation of the test customers (3 to 10 minutes). The orchestrator normally runs this for you.
