"""Turn brief inputs (files and folders of any kind) into a plain-text corpus with an index.

    python builder_v2/ingest.py <out_dir> <path> [<path> ...]

Text files are copied, PDFs converted to text, large JSON files summarised (schema + samples; the full file
stays where it is and its path is listed in the index). Nothing here knows anything about a particular brief.
"""
import json, pathlib, re, shutil, subprocess, sys

TEXT = {".md", ".txt", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".js", ".ts", ".py", ".html", ".xml", ".eml", ".json"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".wrangler"}
BIG_JSON = 60_000


def pdf_text(p):
    try:
        return subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True, encoding="utf-8", timeout=60).stdout
    except Exception:
        try:
            import pypdf
            return "\n\n".join((pg.extract_text() or "") for pg in pypdf.PdfReader(str(p)).pages)
        except Exception as e:
            return f"(could not read PDF: {e})"


def shape(v, depth=0):
    """A compact type sketch of a JSON value."""
    if depth > 6: return "..."
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return [shape(v[0], depth + 1), f"... {len(v)} items"] if v else []
    return type(v).__name__ if not isinstance(v, str) or len(v) > 40 else repr(v)


def json_summary(p, raw):
    try:
        data = json.loads(raw)
    except Exception:
        return raw[:BIG_JSON]
    out = [f"(Large JSON file, {len(raw)} bytes. Full file: {p}. Below: its shape and samples.)", "", "## Shape", json.dumps(shape(data), ensure_ascii=False, indent=1)[:15000]]
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and v:
                out += ["", f"## `{k}`: {len(v)} items. First two in full:", json.dumps(v[:2], ensure_ascii=False, indent=1)[:20000]]
            elif isinstance(v, str):
                out += ["", f"## `{k}`", v]
    elif isinstance(data, list):
        out += ["", f"## {len(data)} items. First two in full:", json.dumps(data[:2], ensure_ascii=False, indent=1)[:20000]]
    return "\n".join(out)


def main(out_dir, paths):
    out = pathlib.Path(out_dir); corpus = out / "corpus"
    if corpus.exists(): shutil.rmtree(corpus)
    corpus.mkdir(parents=True)
    rows = []
    for root in map(pathlib.Path, paths):
        files = [root] if root.is_file() else sorted(f for f in root.rglob("*") if f.is_file() and not (set(f.parts) & SKIP_DIRS))
        for f in files:
            rel = f.name if root.is_file() else str(f.relative_to(root))
            ext = f.suffix.lower()
            if ext == ".pdf":
                text, kind = pdf_text(f), "pdf (converted to text)"
            elif ext in TEXT:
                text = f.read_text(encoding="utf-8", errors="replace")
                kind = "text"
                if ext == ".json" and len(text) > BIG_JSON:
                    text, kind = json_summary(f.resolve(), text), "large json (summarised)"
            else:
                continue
            dest = corpus / (re.sub(r"[\\/]", "__", rel) + ("" if ext in {".md", ".txt"} else ".txt"))
            dest.write_text(text, encoding="utf-8")
            date = re.search(r"(20\d\d-\d\d-\d\d)", rel)
            rows.append((dest.name, str(f.resolve()), kind, len(text), date.group(1) if date else ""))
    lines = ["# Corpus index", "", "Every input file, as text. `original` is the real file; read it when a summary is not enough.", "",
             "| file in corpus | original | kind | chars | date in name |", "|---|---|---|---|---|"]
    for rel, orig, kind, n, d in rows:
        lines.append(f"| {rel} | {orig} | {kind} | {n} | {d} |")
    (out / "corpus_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(rows)} files -> {corpus}")
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 3: sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2:])
