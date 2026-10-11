"""Load `export NAME=value` lines from the kit's env.sh into os.environ (values already set win)."""
import os, pathlib, re
_f = pathlib.Path(__file__).resolve().parent.parent / "env.sh"
if _f.exists():
    for line in _f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*export\s+([A-Za-z_]\w*)=(\S*)", line)
        if m: os.environ.setdefault(m.group(1), m.group(2).strip("'\""))
