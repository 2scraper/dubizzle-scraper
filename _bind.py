"""Bind every shared-module call in every engine against the callee's real
signature (§17 #1).

A sibling repo shipped `classify(html, url=...)` in two of three engines
against `classify(html, status, url)`; both crashed on their FIRST fetch and
nothing short of a live run saw it — not import, not --help, not compileall,
not the undefined-name walk, not 400 green assertions.
"""
import ast, importlib, inspect, sys

CALLERS = ["playwright_scraper.py", "puppeteer_scraper.py",
           "selenium_scraper.py", "scraper_api_client.py", "page_flow.py"]
SHARED = ["product_parser", "page_flow", "output_writer", "proxy_pool",
          "env_config", "captcha_solver", "fingerprint_client"]

mods = {}
for name in SHARED:
    try:
        mods[name] = importlib.import_module(name)
    except ImportError as exc:
        print("skip %s: %s" % (name, exc))


class _P:
    """Stands in for any argument. Never called, only bound."""


def resolve(node, local_imports):
    """The (module, attr) a Call node names, or None."""
    f = node.func
    if isinstance(f, ast.Name) and f.id in local_imports:
        return local_imports[f.id]
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id in mods:
            return (f.value.id, f.attr)
    return None


problems = 0
checked = 0
for path in CALLERS:
    src = open(path).read()
    tree = ast.parse(src)
    local = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in mods:
            for a in n.names:
                local[a.asname or a.name] = (n.module, a.name)
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        target = resolve(n, local)
        if not target:
            continue
        mod, attr = target
        fn = getattr(mods[mod], attr, None)
        if not callable(fn) or inspect.isclass(fn):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        args = [_P() for _ in n.args]
        if any(isinstance(a, ast.Starred) for a in n.args):
            continue
        kwargs = {k.arg: _P() for k in n.keywords if k.arg}
        if any(k.arg is None for k in n.keywords):
            continue
        checked += 1
        try:
            sig.bind(*args, **kwargs)
        except TypeError as exc:
            problems += 1
            print("%s:%d  %s.%s%s  <-  %s" % (path, n.lineno, mod, attr, sig, exc))

print("bound %d shared-module calls, %d problems" % (checked, problems))
sys.exit(1 if problems else 0)
