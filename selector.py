"""Blast Radius selector.

For each patch: work out which code in the pinned sqlglot changed (per function,
by comparing ASTs), select the tests that ran that code at the pinned commit
(from a coverage map built there), and pack them onto workers.
"""
import ast
import json
import os
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", Path(__file__).with_name("map.json")))
MAX_WORKERS = 8
WORKER_MS = 2000



def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- patches

def parse_patch(text):
    """Return list of dicts: old, new (paths or None), hunks [(old_start, lines)]."""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False, "header": True}
            files.append(cur)
            parts = ln.split(" ")
            if len(parts) >= 4:
                cur["old"] = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                cur["new"] = parts[3][2:] if parts[3].startswith("b/") else parts[3]
            i += 1
            continue
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hunks"] or not cur.get("header"):
                cur = {"old": None, "new": None, "hunks": [], "binary": False, "header": False}
                files.append(cur)
            o = ln[4:].split("\t")[0].strip()
            n = lines[i + 1][4:].split("\t")[0].strip()
            cur["old"] = None if o == "/dev/null" else (o[2:] if o.startswith("a/") else o)
            cur["new"] = None if n == "/dev/null" else (n[2:] if n.startswith("b/") else n)
            cur["header"] = False
            i += 2
            continue
        if cur is not None:
            if ln.startswith("new file mode"):
                cur["old"] = None
            elif ln.startswith("deleted file mode"):
                cur["new"] = None
            elif ln.startswith("rename from "):
                cur["old"] = ln[len("rename from "):]
            elif ln.startswith("rename to "):
                cur["new"] = ln[len("rename to "):]
            elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
                cur["binary"] = True
            elif ln.startswith("@@"):
                # Read exactly the line counts the header gives: a removed line
                # that itself starts with "-- " reads as "--- ...", so the body
                # can't be told apart from the next file header by its text.
                head = ln.split("@@")[1].strip().split(" ")
                old = head[0][1:].split(",")
                new = head[1][1:].split(",")
                ostart = int(old[0])
                need_old = int(old[1]) if len(old) > 1 else 1
                need_new = int(new[1]) if len(new) > 1 else 1
                body = []
                i += 1
                while need_old > 0 or need_new > 0 or (i < len(lines) and lines[i].startswith("\\")):
                    if i >= len(lines):
                        raise ValueError("hunk is shorter than its header says")
                    b = lines[i]
                    tag = b[:1] if b else " "
                    if tag == " ":
                        need_old -= 1
                        need_new -= 1
                    elif tag == "-":
                        need_old -= 1
                    elif tag == "+":
                        need_new -= 1
                    elif tag != "\\":
                        raise ValueError("hunk is shorter than its header says")
                    if need_old < 0 or need_new < 0:
                        raise ValueError("hunk is longer than its header says")
                    body.append(b)
                    i += 1
                cur["hunks"].append((ostart, body))
                continue
        i += 1
    return files


def apply_hunks(old_text, hunks):
    old = old_text.split("\n")
    out = []
    pos = 0  # index into old (0-based)
    changed_old_lines = set()
    for ostart, body in hunks:
        start = max(ostart - 1, 0)
        if not any(b[:1] in (" ", "-") or b == "" for b in body):
            start = ostart  # pure insertion ("@@ -N,0"): the new lines go after old line N
        out.extend(old[pos:start])
        pos = start
        for b in body:
            if b.startswith("\\"):
                continue
            tag, content = (b[0], b[1:]) if b else (" ", "")
            if tag == " ":
                if pos >= len(old) or old[pos] != content:
                    raise ValueError("patch context does not match the pinned source")
                out.append(old[pos])
                pos += 1
            elif tag == "-":
                if pos >= len(old) or old[pos] != content:
                    raise ValueError("patch does not match the pinned source")
                changed_old_lines.add(pos + 1)
                pos += 1
            elif tag == "+":
                changed_old_lines.add(pos + 1)  # insertion point
                changed_old_lines.add(pos)
                out.append(content)
    out.extend(old[pos:])
    return "\n".join(out), changed_old_lines


# ---------------------------------------------------------------- AST units
#
# A unit is a function (or lambda), a class body, or the module body. Each unit's
# fingerprint covers its own statements only: nested function and class bodies
# are replaced by a marker, while their headers (name, decorators, defaults,
# bases) stay with the unit that executes them.

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _fp(node, root, out):
    if node is not root:
        if isinstance(node, ast.Lambda):
            out.append("L(")
            _fp(node.args, root, out)
            out.append(")")
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"F({node.name},")
            for d in node.decorator_list:
                _fp(d, root, out)
            _fp(node.args, root, out)
            if node.returns is not None:
                _fp(node.returns, root, out)
            out.append(")")
            return
        if isinstance(node, ast.ClassDef) and not isinstance(root, _FUNC):
            out.append(f"C({node.name},")
            for d in node.decorator_list + node.bases + node.keywords:
                _fp(d, root, out)
            out.append(")")
            return
    out.append(type(node).__name__)
    out.append("(")
    for name, value in ast.iter_fields(node):
        if isinstance(value, list):
            out.append("[")
            for v in value:
                if isinstance(v, ast.AST):
                    _fp(v, root, out)
                else:
                    out.append(repr(v))
                out.append(",")
            out.append("]")
        elif isinstance(value, ast.AST):
            _fp(value, root, out)
        else:
            out.append(repr(value))
        out.append(",")
    out.append(")")


def fingerprint(node):
    out = []
    _fp(node, node, out)
    return "".join(out)


def units(tree):
    """key -> (kind, node, qualname, firstlineno). Keys are stable across edits."""
    res = {("module",): ("module", tree, "", 0)}

    def visit(node, prefix, keypath, in_func, counts):
        if isinstance(node, _FUNC):
            name = "<lambda>" if isinstance(node, ast.Lambda) else node.name
            c = counts.get(name, 0)
            counts[name] = c + 1
            key = keypath + (f"{name}#{c}",)
            first = node.lineno
            if not isinstance(node, ast.Lambda) and node.decorator_list:
                first = min([d.lineno for d in node.decorator_list] + [node.lineno])
                for d in node.decorator_list:
                    visit(d, prefix, keypath, in_func, counts)
            if not isinstance(node, ast.Lambda):
                # defaults run in the enclosing scope; a lambda there is its own unit
                for d in node.args.defaults + [x for x in node.args.kw_defaults if x]:
                    visit(d, prefix, keypath, in_func, counts)
            res[key] = ("func", node, prefix + name, first)
            sub = {}
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            for st in body:
                visit(st, prefix + name + ".<locals>.", key, True, sub)
            return
        if isinstance(node, ast.ClassDef):
            name = node.name
            c = counts.get("class " + name, 0)
            counts["class " + name] = c + 1
            for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                visit(d, prefix, keypath, in_func, counts)
            if in_func:
                sub = {}
                for st in node.body:
                    visit(st, prefix + name + ".", keypath + (f"class {name}#{c}",), True, sub)
                return
            key = keypath + (f"class {name}#{c}",)
            res[key] = ("class", node, prefix + name, node.lineno)
            sub = {}
            for st in node.body:
                visit(st, prefix + name + ".", key, False, sub)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, prefix, keypath, in_func, counts)

    top = {}
    for st in tree.body:
        visit(st, "", (), False, top)
    return res


def _start(node):
    return min([d.lineno for d in getattr(node, "decorator_list", ())] + [node.lineno])


def same_text(ol, on, nl, nn):
    """True when two units are written identically: the same source lines, from
    the first decorator to the last line, with the node starting and ending at
    the same columns. Identical source parses to an identical AST, so the
    fingerprints would match; this only skips computing them."""
    if isinstance(on, ast.Module) or isinstance(nn, ast.Module):
        return False
    a, b = _start(on), on.end_lineno
    c, d = _start(nn), nn.end_lineno
    if b - a != d - c or on.lineno - a != nn.lineno - c:
        return False
    if on.col_offset != nn.col_offset or on.end_col_offset != nn.end_col_offset:
        return False
    return ol[a - 1:b] == nl[c - 1:d]


# ---------------------------------------------------------------- statement spans
#
# Inside a changed function, find the old statements whose behaviour changed.
# A test can only notice the change if it ran one of those statements (or, for
# inserted statements, entered the block they were inserted into).

_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def _stmt_fp(node):
    return fingerprint_in(node)


_IN_FUNC = ast.Lambda(args=ast.arguments(), body=ast.Constant(None))  # a root that keeps nested class bodies, strips nested functions


def fingerprint_in(node):
    out = []
    _fp(node, _IN_FUNC, out)
    return "".join(out)


def _header_fp(node):
    out = [type(node).__name__]
    for name, value in ast.iter_fields(node):
        if name in _BLOCK_FIELDS:
            continue
        if isinstance(value, list):
            for v in value:
                if isinstance(v, ast.AST):
                    _fp(v, _IN_FUNC, out)
                else:
                    out.append(repr(v))
        elif isinstance(value, ast.AST):
            _fp(value, _IN_FUNC, out)
        else:
            out.append(repr(value))
        out.append(",")
    return "".join(out)


def _silent(st):
    """Statements that compile to no bytecode, so no test is ever recorded on their line."""
    return isinstance(st, (ast.Global, ast.Nonlocal)) or (isinstance(st, ast.AnnAssign) and st.value is None)


def _no_code(st):
    return (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)) or _silent(st)


class _Whole(Exception):
    pass


def _entry(block, spans):
    """Mark entering `block`: any line of it counts, since its first line may
    have no line event (a multi-line `if (`, an annotation-only `x: T`, `global`)."""
    if all(_silent(st) for st in block):
        raise _Whole()
    spans.append((block[0].lineno, block[-1].end_lineno))


def _diff_block(ob, nb, spans, top):
    import difflib
    of = [_stmt_fp(s) for s in ob]
    nf = [_stmt_fp(s) for s in nb]
    sm = difflib.SequenceMatcher(None, of, nf, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            if top:
                raise _Whole()
            _entry(ob, spans)
            continue
        olds = ob[i1:i2]
        if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1 and type(olds[0]) is type(nb[j1]) and not isinstance(
            olds[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and any(
            isinstance(getattr(olds[0], f, None), list) and f in _BLOCK_FIELDS for f in ("body",)
        ):
            o, n = olds[0], nb[j1]
            if _header_fp(o) != _header_fp(n):
                first_body = o.body[0].lineno if getattr(o, "body", None) else o.end_lineno
                spans.append((o.lineno, max(o.lineno, first_body - 1)))
            for f in _BLOCK_FIELDS:
                ov, nv = getattr(o, f, None), getattr(n, f, None)
                if ov is None and nv is None:
                    continue
                if f in ("handlers", "cases"):
                    if len(ov or []) != len(nv or []):
                        spans.append((o.lineno, o.end_lineno))
                        break
                    for oh, nh in zip(ov, nv):
                        if _header_fp(oh) != _header_fp(nh):
                            spans.append((oh.lineno, oh.end_lineno))
                        else:
                            _diff_block(oh.body, nh.body, spans, False)
                    continue
                if not ov and not nv:
                    continue
                if not ov:  # new else/finally block
                    spans.append((o.lineno, o.end_lineno))
                    continue
                if not nv:  # removed block
                    spans.append((ov[0].lineno, ov[-1].end_lineno))
                    continue
                _diff_block(ov, nv, spans, False)
            continue
        for st in olds:
            if _no_code(st):
                if top:
                    raise _Whole()
                _entry(ob, spans)
            else:
                spans.append((st.lineno, st.end_lineno))


def _scope_sig(fn):
    """What makes a function's behaviour depend on its whole body, not only the
    statements that ran: generator/coroutine status and which names are local,
    global or nonlocal. Nested function and class bodies are their own scopes."""
    gen = False
    bound, decl = set(), set()

    def walk(node, top):
        nonlocal gen
        if not top:
            if isinstance(node, ast.Lambda):
                for d in node.args.defaults + [x for x in node.args.kw_defaults if x]:
                    walk(d, False)
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
                parts = list(node.decorator_list)
                if isinstance(node, ast.ClassDef):
                    parts += node.bases + [k.value for k in node.keywords]
                else:
                    a = node.args
                    parts += a.defaults + [x for x in a.kw_defaults if x]
                for d in parts:
                    walk(d, False)
                return
        if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)):
            gen = True
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            decl.update(f"{type(node).__name__}:{n}" for n in node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # comprehension variables stay inside it; := still binds here
            for c in node.generators:
                walk(c.iter, False)
                for x in c.ifs:
                    walk(x, False)
            elts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            for x in elts:
                walk(x, False)
            for x in ast.walk(node):
                if isinstance(x, ast.NamedExpr):
                    bound.add(x.target.id)
            return
        for c in ast.iter_child_nodes(node):
            walk(c, False)

    for st in ([fn.body] if isinstance(fn, ast.Lambda) else fn.body):
        walk(st, False)
    if not isinstance(fn, ast.Lambda):
        a = fn.args
        for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
            if x is not None:
                bound.add(x.arg)
    return (type(fn).__name__, gen, frozenset(bound), frozenset(decl))


_PURE = (ast.Constant, ast.Name, ast.Attribute, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Starred,
         ast.UnaryOp, ast.BinOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Lambda, ast.arguments, ast.arg,
         ast.Load, ast.operator, ast.unaryop, ast.boolop, ast.cmpop)


def _pure_value(v):
    """A value whose evaluation can only bind its own name: literals, containers,
    name and attribute loads, operators, lambdas (whose bodies are their own
    units). No calls, walrus, subscripts, comprehensions or f-strings, which
    could change other state while the class body runs. A bare name or
    attribute (an alias of another, possibly shared, object) is not accepted."""
    if isinstance(v, (ast.Name, ast.Attribute)):
        return False

    def ok(n):
        if not isinstance(n, _PURE):
            return False
        if isinstance(n, ast.Lambda):
            return ok(n.args)
        return all(ok(c) for c in ast.iter_child_nodes(n))

    return ok(v)


def changed_class_attrs(old_c, new_c):
    """Names whose value changed when a class body differs only in plain
    `NAME = value` statements (same names, same order, one target each, no
    dunders, side-effect-free values before and after), with the same header
    and no decorators; else None."""
    if old_c.decorator_list or new_c.decorator_list:
        return None
    hdr = lambda c: [ast.dump(x) for x in c.bases + c.keywords + getattr(c, "type_params", [])]
    if old_c.name != new_c.name or hdr(old_c) != hdr(new_c) or len(old_c.body) != len(new_c.body):
        return None
    names = set()
    for so, sn in zip(old_c.body, new_c.body):
        fo, fn = [], []
        _fp(so, old_c, fo)
        _fp(sn, new_c, fn)
        if fo == fn:
            continue
        ok = all(
            isinstance(x, ast.Assign) and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name)
            for x in (so, sn)
        )
        if not ok or so.targets[0].id != sn.targets[0].id:
            return None
        if not (_pure_value(so.value) and _pure_value(sn.value)):
            return None
        n = so.targets[0].id
        if n.startswith("__") and n.endswith("__"):
            return None
        names.add(n)
    return names or None


def changed_spans(old_fn, new_fn):
    """Old line spans that changed inside a function, or None for the whole function."""
    if isinstance(old_fn, ast.Lambda) or isinstance(new_fn, ast.Lambda):
        return None
    if _header_fp(old_fn) != _header_fp(new_fn):
        return None
    if _scope_sig(old_fn) != _scope_sig(new_fn):
        return None  # e.g. return -> yield, or a new local name: every call changes
    spans = []
    try:
        _diff_block(old_fn.body, new_fn.body, spans, True)
    except _Whole:
        return None
    return spans or None


# ---------------------------------------------------------------- map

class Map:
    def __init__(self, path):
        m = json.loads(path.read_text())
        self.sets = m["sets"]
        self.codes = m["codes"]
        self.lines = m["lines"]
        self.imports = m["imports"]
        self.opens = m["opens"]
        self.classes = m.get("classes", {})
        self.n = m["n"]
        try:
            a = json.loads(path.with_name("attr_reads.json").read_text())
            self.attr_sets, self.attr_names = a["sets"], a["names"]
        except (OSError, ValueError, KeyError):
            self.attr_sets, self.attr_names = [], {}
        self._code_index = {}
        self._bits = {}

    def bits(self, sid):
        v = self._bits.get(sid)
        if v is None:
            v = self._bits[sid] = int(self.sets[sid], 16)
        return v

    def code_tests(self, f, qual, first):
        idx = self._code_index.get(f)
        if idx is None:
            idx = {}
            for k, sid in self.codes.get(f, {}).items():
                q, _, l = k.rpartition("@")
                idx.setdefault(int(l), []).append((q, sid))
            self._code_index[f] = idx
        out = 0
        found = False
        name = qual.rsplit(".", 1)[-1]
        for q, sid in idx.get(first, []):
            if q == qual or q.rsplit(".", 1)[-1] == name:
                out |= self.bits(sid)
                found = True
        return out, found

    def class_tests(self, f, qual):
        """Tests that instantiated qual (or a subclass), or ran one of its methods."""
        out = 0
        found = False
        sid = self.classes.get(f, {}).get(qual)
        if sid is not None:
            out |= self.bits(sid)
            found = True
        pre = qual + "."
        for k, sid in self.codes.get(f, {}).items():
            if k.startswith(pre):
                out |= self.bits(sid)
        return out, found

    def has_lines(self, f):
        return bool(self.lines.get(f))

    def span_tests(self, f, a, b):
        d = self.lines.get(f, {})
        out = 0
        hit = False
        for ln in range(a, b + 1):
            sid = d.get(str(ln))
            if sid is not None:
                out |= self.bits(sid)
                hit = True
        return out, hit

    def attr_readers(self, names):
        """Tests that ran a line that could read any of `names` (by name, by the
        string constant, or by reflective access), or None if unknown."""
        out = 0
        for n in names:
            i = self.attr_names.get(n)
            if i is None:
                return None
            out |= int(self.attr_sets[i], 16)
        return out

    def importers(self, f):
        sid = self.imports.get(f)
        return self.bits(sid) if sid is not None else 0

    def readers(self, f):
        sid = self.opens.get(f)
        return self.bits(sid) if sid is not None else 0


# ---------------------------------------------------------------- selection

ALL = -1  # every bit set


def count(bits):
    return -1 if bits < 0 else bin(bits).count("1")


DOC_PREFIXES = (".github/", "posts/", "pdoc/", "benchmarks/", "sqlglot-integration-tests/")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".png", ".gif", ".jpg", ".svg", ".jinja2")
DOC_FILES = {"LICENSE", ".gitignore", ".gitpod.yml", ".pre-commit-config.yaml", "MANIFEST.in", "Makefile", "CHANGELOG.md"}


def select(patch_text, mp, why):
    sel = 0
    pending = []  # class-body changes: (file, class, its users)
    patched = {}  # file -> new text, for the import check
    for fd in parse_patch(patch_text):
        old, new = fd["old"], fd["new"]
        path = old or new
        if path is None:
            continue
        for p in {old, new} - {None}:
            sel |= mp.readers(p)
        if fd["binary"]:
            if path.startswith(DOC_PREFIXES) or path.endswith(DOC_SUFFIXES):
                why.append(f"{path}: binary doc asset")
                continue
            why.append(f"{path}: binary -> all")
            return ALL
        if not path.endswith(".py"):
            if path.startswith("tests/fixtures/") or mp.readers(path):
                why.append(f"{path}: data file, {count(mp.readers(path))} readers")
                continue
            if old is None and path.startswith("tests/"):
                why.append(f"{path}: new non-py test file")
                continue
            if path in DOC_FILES or path.startswith(DOC_PREFIXES) or (
                path.endswith(DOC_SUFFIXES) and not path.startswith(("sqlglot/", "tests/"))
            ):
                why.append(f"{path}: not read by tests")
                continue
            why.append(f"{path}: unknown non-py -> all")
            return ALL
        if path.startswith(DOC_PREFIXES):
            why.append(f"{path}: outside the tested code")
            continue
        if old is None:
            if path.startswith("tests/") and not path.endswith(("conftest.py", "__init__.py")):
                why.append(f"{path}: new test module")
                continue
            why.append(f"{path}: new module -> all")
            return ALL
        try:
            old_text = (SRC / old).read_text()
        except OSError:
            why.append(f"{old}: not in source -> all")
            return ALL
        if new is None or new != old:
            if new is not None:
                why.append(f"{old}: renamed -> all")
                return ALL
            t = mp.importers(old)
            sel |= t
            why.append(f"{old}: deleted -> importers {count(t)}")
            continue
        new_text, _ = apply_hunks(old_text, fd["hunks"])
        try:
            old_tree = ast.parse(old_text)
        except SyntaxError:
            return ALL
        try:
            new_tree = ast.parse(new_text)
            # ast.parse skips the compiler's own checks ('break' outside a loop,
            # duplicate arguments, bad nonlocal): importing the module would fail.
            compile(new_tree, old, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:
            t = mp.importers(old)
            sel |= t
            why.append(f"{old}: does not compile ({type(e).__name__}) -> importers {count(t)}")
            continue
        patched[old] = new_text
        old_lines, new_lines = old_text.split("\n"), new_text.split("\n")
        ou, nu = units(old_tree), units(new_tree)
        metas = {n.name for n in old_tree.body if isinstance(n, ast.ClassDef) and any(
            isinstance(b, ast.Name) and b.id == "type" for b in n.bases)}
        for key in sorted(set(ou) | set(nu), key=len):
            if key not in ou:
                continue  # added unit: its parent's fingerprint changed and covers it
            kind, node, q, first = ou[key]
            if key in nu and nu[key][0] == kind and (
                same_text(old_lines, node, new_lines, nu[key][1])
                or fingerprint(node) == fingerprint(nu[key][1])
            ):
                continue
            if kind == "module":
                t = mp.importers(old)
                why.append(f"{old}: module level -> importers {count(t)}")
            elif kind == "class":
                # A class body (its statements, decorators, method defaults) runs
                # when the module is imported, before any instance exists. Narrow
                # to the class's users only once the patched code is shown to
                # import cleanly (checked after every file is read).
                t, found = mp.class_tests(old, q)
                if not found:
                    # never seen instantiated (enums, dataclasses, namespaces): its
                    # users are unknown, so every importer runs
                    t |= mp.importers(old)
                    sel |= t
                    why.append(f"{old}: class {q} body, no recorded instances -> importers {count(t)}")
                    continue
                # Narrower still when only plain attribute values changed: the
                # class's users that also ran a line that could read one of them.
                # Applied only if the import check passes and finds a plain class.
                nt = None
                names = changed_class_attrs(node, nu[key][1]) if key in nu and nu[key][0] == "class" else None
                if names:
                    r = mp.attr_readers(names)
                    if r is not None:
                        nt = t & r
                        why.append(f"{old}: class {q} attrs {sorted(names)}: {count(nt)} of {count(t)} users read them")
                pending.append((old, q, t, nt))
                continue
            else:
                t, found = mp.code_tests(old, q, first)
                spans = None
                if found and t and mp.has_lines(old) and key in nu and nu[key][0] == "func":
                    spans = changed_spans(node, nu[key][1])
                if spans is not None:
                    st = 0
                    for a, b in spans:
                        x, _ = mp.span_tests(old, a, b)
                        st |= x
                    st &= t
                    why.append(f"{old}: {q}@{first} lines {spans} -> {count(st)} of {count(t)}")
                    t = st
                else:
                    why.append(f"{old}: {q}@{first} -> {count(t)}{'' if found else ' (never ran)'}")
                if q.split(".", 1)[0] in metas and not q.endswith(".__new__"):
                    # metaclass methods act on class-level state shared between tests
                    t |= mp.importers(old)
                    why.append(f"{old}: {q} is a metaclass method -> importers {count(t)}")
            sel |= t
    if pending:
        mods = sorted({f for f, _, _, _ in pending})
        users = sel
        for f, q, t, nt in pending:
            users |= t
        narrow = sel
        for f, q, t, nt in pending:
            narrow |= t if nt is None else nt
        if narrow == users:  # narrowing would change nothing
            pending = [(f, q, t, None) for f, q, t, _ in pending]
        plain = set()
        if narrow == users and all(users | mp.importers(f) == users for f, _, _, _ in pending):
            # every importer is already selected: the check cannot change the answer
            why.append("import check skipped: importers already selected")
            ok = True
        else:
            probe = [(f, q) for f, q, _, nt in pending if nt is not None]
            ok = imports_cleanly(patched, mods, why, probe, plain)
        for f, q, t, nt in pending:
            if not ok:
                t |= mp.importers(f)
            elif nt is not None and (f, q) in plain:
                t = nt
            sel |= t
            why.append(f"{f}: class {q} body -> {'importers' if not ok else 'attr readers' if t is nt else 'users'}, {count(t)}")
    return sel


_CHECK = r"""
import importlib, importlib.util, json, sys
ovl, root, mods, probe = json.loads(sys.argv[1])
sys.path.insert(0, root)

class Finder:
    @staticmethod
    def find_spec(name, path=None, target=None):
        f = ovl.get(name)
        if f is None:
            return None
        pkg = f["pkg"]
        return importlib.util.spec_from_file_location(
            name, f["file"], submodule_search_locations=[pkg] if pkg else None)

sys.meta_path.insert(0, Finder)
for m in mods:
    importlib.import_module(m)
# A clean import of the changed module says nothing about the modules built on
# it (a dialect can fail on a changed base class), so import every module.
import pkgutil, sqlglot
failed = []
for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(info.name)
    except BaseException as e:
        failed.append(info.name)
# Which of the probed classes are plain: not an enum, dataclass or tuple, and
# every class in its MRO and its metaclass's MRO comes from sqlglot, builtins or
# typing (so no attribute value is read by code the attribute index did not
# scan). abc is excluded: ABCMeta reads values to find abstract methods.
plain = []
for mod, qual in probe:
    try:
        obj = sys.modules[mod]
        for part in qual.split("."):
            obj = getattr(obj, part)
        ok_mod = lambda m: m in ("builtins", "typing") or m == "sqlglot" or m.startswith("sqlglot.")
        if (isinstance(obj, type) and all(ok_mod(k.__module__) for k in obj.__mro__)
                and all(ok_mod(k.__module__) for k in type(obj).__mro__)
                and not any("__dataclass_fields__" in vars(k) for k in obj.__mro__)
                and not issubclass(obj, tuple)):
            plain.append([mod, qual])
    except BaseException:
        pass
print("IMPORT-OK " + json.dumps([sorted(failed), plain]))
"""


def _modname(path):
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


_BASELINE = []
BASELINE_FILE = Path(os.environ.get("BR_BASELINE", Path(__file__).with_name("import_baseline.json")))


def write_baseline():
    """Build time: record which sqlglot modules fail to import on the pinned
    source (sqlglot.__main__ needs an installed version), with the same check
    the selector runs, so the run does not repeat it."""
    ok, res = _run_check({}, [])
    if not ok:
        raise SystemExit(f"pinned source does not import: {res}")
    base = res[0]
    BASELINE_FILE.write_text(json.dumps(sorted(base)))
    print("import baseline:", sorted(base))


def _load_baseline():
    try:
        return set(json.loads(BASELINE_FILE.read_text()))
    except (OSError, ValueError):
        return None


def _run_check(patched, files, probe=()):
    """(clean, (modules that failed, plain probed classes)) for an import of
    `files` with `patched` swapped in."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ovl = {}
        for i, (f, text) in enumerate(sorted(patched.items())):
            if not f.endswith(".py"):
                continue
            dst = Path(tmp) / f"m{i}.py"
            dst.write_text(text)
            pkg = str(SRC / f.rsplit("/", 1)[0]) if f.endswith("/__init__.py") else None
            ovl[_modname(f)] = {"file": str(dst), "pkg": pkg}
        arg = json.dumps([ovl, str(SRC), [_modname(f) for f in files], [[_modname(f), q] for f, q in probe]])
        r = subprocess.run(
            [sys.executable, "-S", "-c", _CHECK, arg],
            capture_output=True, text=True, timeout=60, cwd=tmp,
            env={"PYTHONDONTWRITEBYTECODE": "1", "HOME": tmp, "PATH": os.environ.get("PATH", "")},
        )
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or not last.startswith("IMPORT-OK "):
        return False, (r.stderr.strip().splitlines() or ["?"])[-1][:120]
    failed, plain = json.loads(last[len("IMPORT-OK "):])
    return True, (set(failed), {(m, q) for m, q in plain})


def imports_cleanly(patched, files, why, probe=(), plain=None):
    """Import the changed modules, then every sqlglot module, with every patched
    file swapped in, in a fresh interpreter. True only if the changed modules
    import and no module fails that also imports on the pinned source."""
    if not all(f.startswith("sqlglot/") for f in files):
        why.append("import check: not a sqlglot module")
        return False
    try:
        if not _BASELINE:
            base = _load_baseline()
            if base is not None:
                _BASELINE.append(base)
        if not _BASELINE:
            ok, base = _run_check({}, [])
            if not ok:
                why.append(f"import check: pinned source does not import ({base})")
                return False
            _BASELINE.append(base[0])
        ok, res = _run_check(patched, files, probe)
        if not ok:
            why.append(f"import check failed: {res}")
            return False
        res, found_plain = res
        if plain is not None:
            plain.update((f, q) for f, q in probe if (_modname(f), q) in found_plain)
        extra = res - _BASELINE[0]
        if extra:
            why.append(f"import check: {sorted(extra)[0]} fails to import")
            return False
        return True
    except Exception as e:
        why.append(f"import check error {e!r}")
        return False


def pack(numbers, dur):
    if not numbers:
        return []
    items = sorted(numbers, key=lambda n: -dur[n])
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(items):
            break
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            ws[i].append(n)
            loads[i] += dur[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    mp = Map(MAP)
    # The map is keyed by test id; translate to this tests.json's numbers.
    # Tests the map has never seen have no coverage, so they always run.
    ids = json.loads(MAP.with_name("map_ids.json").read_text())
    pos = {tid: i for i, tid in enumerate(ids)}
    known = [(pos[t["id"]], t["number"]) for t in tests if t["id"] in pos]
    unknown = {t["number"] for t in tests if t["id"] not in pos}
    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        why = []
        try:
            bits = select(patch.read_text(errors="replace"), mp, why)
            if bits < 0:
                chosen = set(dur)
            else:
                chosen = {num for i, num in known if bits >> i & 1} | unknown
        except Exception as e:  # never fail the run: run everything
            why.append(f"error {e!r} -> all")
            chosen = set(dur)
        ws = pack(chosen, dur)
        out.append(json.dumps({"commit": patch.stem, "workers": ws}, separators=(",", ":")))
        log(patch.stem, len(chosen), "; ".join(why))
    if out:
        sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
