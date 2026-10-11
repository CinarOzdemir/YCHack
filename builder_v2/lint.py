"""Static checks for a Freya workflow JSON: the mistakes the platform accepts silently and that break calls.

    python builder_v2/lint.py <workflow.json>      exit 1 and a list of problems when something is wrong

Only platform rules live here (from docs/workflow-format.md and docs/tools-and-code.md), nothing from any brief.
"""
import json, re, sys

VAR = re.compile(r"\{\{\s*([A-Za-z_][\w.\-]*)(?::json)?\s*\}\}")


def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items(): yield from walk(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o): yield from walk(v, f"{path}[{i}]")
    yield path, o


def lint(w):
    err, warn = [], []
    nodes = w.get("nodes") or {}
    gnodes = w.get("global_nodes") or {}
    allnodes = {**nodes, **gnodes}
    if w.get("initial_node") not in nodes: err.append(f"initial_node {w.get('initial_node')!r} is not a node")
    if "agent_config" in w: warn.append("agent_config present: leave it out")
    for p, v in walk(w):
        if v is None: warn.append(f"null value at {p} (schema rejects null; omit the field)")
        if isinstance(v, dict) and "const" in v and p.count("parameters"): err.append(f"`const` at {p}: use `default`, const never bakes a value")

    # variables that something sets: call vars, test vars, extraction, mappings, code writes, tool args/responses
    sets = set((w.get("test_variables") or {}).keys())
    code_all = ""
    for n in allnodes.values():
        for e in n.get("extraction_schema") or []: sets.add(e.get("name"))
        for m in n.get("response_mapping") or []: sets.add(m.get("variable"))
        for t in (n.get("tools") or []) + (n.get("pre_actions") or []):
            for m in t.get("response_mapping") or []: sets.add(m.get("variable"))
            code = (((t.get("function") or {}).get("parameters") or {}).get("properties") or {}).get("code", {}).get("default", "")
            code_all += code + "\n" + (t.get("post_process_code") or "") + "\n" + (t.get("pre_process_code") or "")
    sets |= set(re.findall(r"vars\[\s*['\"]([\w.]+)['\"]\s*\]\s*=", code_all))
    sets |= set(re.findall(r"vars\.update\(", code_all) and ["*"] or [])
    sets |= set(re.findall(r"vars\.setdefault\(\s*['\"]([\w.]+)['\"]", code_all))

    def known(v):
        root = v.split(".")[0]
        return (v in sets or root in ("system", "call", "extractor", "date", "time") or "*" in sets
                or re.search(r"_(response|args)$", root) or re.search(r"_(success_count|failure_count|status_code)$", root)
                or root.endswith(("current_turns", "total_turns")) or v.split(".")[-1] in ("current_turns", "total_turns"))

    end_nodes = set()
    for key, n in allnodes.items():
        where = f"node {key}"
        t = n.get("node_type", "conversation")
        if n.get("name") and n["name"] != key: warn.append(f"{where}: name {n['name']!r} differs from key")
        if t == "conversation" and n.get("pre_actions"): err.append(f"{where}: pre_actions on a conversation node are dropped. Use an api_request/code node before it")
        if t != "conversation" and n.get("tools"): err.append(f"{where}: tools on a {t} node are dropped")
        for tool in n.get("pre_actions") or []:
            if tool.get("response_mapping"): err.append(f"{where}: response_mapping inside a pre_actions entry is ignored; put it on the node")
        for tool in (n.get("tools") or []) + (n.get("pre_actions") or []):
            f = tool.get("function") or {}
            if f.get("handler") == "__function__:api_call":
                props = (f.get("parameters") or {}).get("properties") or {}
                for req in ("url", "method"):
                    if "default" not in (props.get(req) or {}): err.append(f"{where}: tool {f.get('name')} has no default {req}")
                body = props.get("body") or {}
                if "default" in body and not body.get("properties"): err.append(f"{where}: tool {f.get('name')} body is one default object; declare each field under body.properties")
                for m in tool.get("response_mapping") or n.get("response_mapping") or []:
                    if not str(m.get("path", "")).startswith("data"): warn.append(f"{where}: mapping path {m.get('path')!r} should start with 'data.'")
                if n.get("node_type") == "conversation" and not f.get("description"): err.append(f"{where}: tool {f.get('name')} has no description; the model will not know when to call it")
            if f.get("handler") == "__function__:code_exec":
                code = (((f.get("parameters") or {}).get("properties") or {}).get("code") or {}).get("default", "")
                if "{{" in code or "}}" in code: err.append(f"{where}: code contains '{{{{' or '}}}}' (templating breaks Python). Read values from vars, and split nested dict literals")
                try: compile(code, key, "exec")
                except SyntaxError as e: err.append(f"{where}: code does not compile: {e}")
            for hook in ("pre_process_code", "post_process_code"):
                if tool.get(hook):
                    try: compile(tool[hook], f"{key}.{f.get('name')}.{hook}", "exec")
                    except SyntaxError as e: err.append(f"{where}: {hook} of {f.get('name')} does not compile: {e}")
        if t == "end_call":
            end_nodes.add(key)
            if not n.get("terminal"): err.append(f"{where}: end_call node without terminal")
            if n.get("transitions"): warn.append(f"{where}: end_call node should have transitions []")
        trs = n.get("transitions") or []
        if n.get("passthrough"):
            if not trs or (trs[-1].get("condition") or {}).get("operator") != "always": err.append(f"{where}: passthrough node must end with an `always` transition (else deadlock)")
            if not n.get("passthrough_default_transition"): err.append(f"{where}: passthrough node without passthrough_default_transition")
        for tr in trs:
            tgt = tr.get("target_node")
            if tgt != "__previous__" and tgt not in allnodes: err.append(f"{where}: transition to unknown node {tgt!r}")
            c = tr.get("condition") or {}
            if c.get("logic") == "and": err.append(f"{where}: top-level 'and' condition; wrap it in an 'or' group")
            for p, v in walk(c):
                if isinstance(v, dict) and v.get("operator") == "equals" and str(v.get("operand")) in ("true", "True", "false", "False"):
                    other = {"true": "True", "True": "true", "false": "False", "False": "false"}[str(v["operand"])]
                    sib = json.dumps(c)
                    if f'"operand": "{other}"' not in sib: warn.append(f"{where}: boolean test on {v.get('variable')} only as {v['operand']!r}; also test {other!r}")
                if isinstance(v, dict) and v.get("variable") and not known(v["variable"]):
                    warn.append(f"{where}: condition reads {v['variable']!r} which nothing visibly sets")
        if n.get("max_node_turns") and not n.get("max_turn_transition"): warn.append(f"{where}: max_node_turns without max_turn_transition does nothing")
        if n.get("max_turn_transition") and n["max_turn_transition"] not in allnodes: err.append(f"{where}: max_turn_transition to unknown node")
        for e in n.get("extraction_schema") or []:
            if "min_confidence" not in e: warn.append(f"{where}: extraction {e.get('name')} without min_confidence (default 0.95 discards most values)")
        for fld in ("task_message", "loop_prompt"):
            for v in VAR.findall(n.get(fld) or ""):
                if not known(v): warn.append(f"{where}: {fld} uses {{{{{v}}}}} which nothing visibly sets (the model would see the placeholder)")
        oe = n.get("on_enter") or {}
        for v in VAR.findall(oe.get("text") or ""):
            if not known(v): err.append(f"{where}: on_enter text speaks {{{{{v}}}}} which nothing sets")
    # reachability: every node reachable from initial, some end node reachable
    seen, todo = set(), [w.get("initial_node")]
    while todo:
        k = todo.pop()
        if k in seen or k not in nodes: continue
        seen.add(k); n = nodes[k]
        todo += [t.get("target_node") for t in n.get("transitions") or []] + [n.get("passthrough_default_transition"), n.get("max_turn_transition")]
    for g in gnodes.values():
        todo = [t.get("target_node") for t in g.get("transitions") or []]
        while todo:
            k = todo.pop()
            if k in seen or k not in nodes: continue
            seen.add(k); todo += [t.get("target_node") for t in nodes[k].get("transitions") or []]
    for k in nodes:
        if k not in seen: warn.append(f"node {k} is unreachable from initial_node")
    if not end_nodes: warn.append("no end_call node: calls never end cleanly")
    return err, warn


def main(path):
    w = json.load(open(path, encoding="utf-8"))
    if "workflow" in w and "nodes" not in w: w = w["workflow"]
    err, warn = lint(w)
    for e in err: print("ERROR  ", e)
    for x in warn: print("WARNING", x)
    print(f"lint: {len(err)} errors, {len(warn)} warnings")
    return 1 if err else 0


if __name__ == "__main__":
    if len(sys.argv) != 2: sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
