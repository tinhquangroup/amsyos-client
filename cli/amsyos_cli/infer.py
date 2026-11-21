"""What a program takes and makes, read from its code, for the card `amsyos init` proposes
(spec v3 §7.1; T2, 2026-11-20: a program that read `job.json` itself got `inputs: {}` and
one output `result: txt`).

Python is read by its syntax tree:
- the `amsyos` module (`job.params[...]`, `job.input("x")`, `job.output("x", ".png")`);
- `job.json` read by hand: a name bound to `<...>.get("params")` / `["params"]` (or
  `"inputs"`), then `.get("x", default)` / `["x"]` on it;
- argparse (`add_argument("--x", type=int, default=..., choices=[...], help=...)`);
- the command line read by hand: `sys.argv[1]` (input `input`, `sys.argv[2]` input
  `input_2`) and argparse's positional arguments (named by their dest). A job gets no
  command line, so each is marked `check` (card.py writes it `<fill: check this: ...>`):
  a file input when the value is opened (`open(...)`, `Path(...)`, `read_csv(...)`), or
  its name or help says a file; a str input otherwise (T2, 2026-11-20: `sys.argv[1]` was
  not seen at all);
- the files it writes: `out / "chart.png"`, `"out/chart.png"`, `os.path.join(out, "x.json")`.
Around a param it reads its type (`int(...)`, a literal default), whether it is required
(`if not x:`), its values (`x not in ("line", "bar")`, `choices=[0, 90]`) and a description
(`"x: ..."`).

Across the modules the entry file imports from the repository (card.py `local_modules`):
what one module hands a function of another (`edit(img, job.params)`, `apply(job)`,
`edit(img, **job.params)`) makes that function's own argument a holder of the params, the
job, or each of its keyword arguments a param (`passes`, then `python(..., passes)`;
A customer, 2026-11-20: 11 params read in an imported module were not seen). A function
imported under another name (`from motion import make_video as render`), a class
(`Motion(job.params)`: its `__init__`, then `self.params.get(...)`; `Settings(**job.params)`:
its fields), `{**job.params}` and `job.params.copy()` hold them too (a customer, 2026-11-20,
photo-motion: 5 params read in motion.py were not seen by 0.3.7; these shapes were still
missed by 0.3.9).

A file input is optional when the code runs without it (a customer, 2026-11-20: `audio` was
proposed required though the code handled `job.input("audio")` being None): its value
compared with None or tested (`if audio:`, `audio or ...`), read with `inputs.get(...)`, or
handed to a function whose argument has a default; unless that test ends the job
(`if image is None: job.fail(...)`, `raise`, `sys.exit`).

The params merged into a dict of defaults (`p = {**DEFAULTS, **params}`,
`dict(DEFAULTS, **params)`, `DEFAULTS | params`, `p = dict(DEFAULTS); p.update(params)`):
each key of `DEFAULTS` (a module-level dict literal) is a param with that default, typed
from it; a key read from the merged dict but not in `DEFAULTS` is required unless read with
`.get` (a customer, 2026-11-21, photo.edit: 1 input of 12 was proposed). The params filtered
on the way flow through the same (a customer's feedback: `{**DEFAULTS, **{k: v for k, v in
params.items() if v is not None}}` gave 1 input of 13): a comprehension over a holder's
entries, whatever it filters (`{k: params[k] for k in params if ...}` too), and as pairs to
`dict(...)` or `.update(...)` (`(k, v) for k, v in params.items() if ...`,
`filter(f, params.items())`).

A default of empty text (`"watermark": ""`, `.get("x", "")`, `default=""`) is kept: card.py
writes it `default: ''`, an optional text input (a customer's feedback). A param tested against a
module-level tuple, list, set or dict (`p["crop"] not in CROPS`, `in FILTERS.keys()`) takes
those values (its keys) as its enum (a customer's feedback), as an inline one does.

The programs it runs (`subprocess.run(["ffmpeg", ...])`, `Popen`, `check_output`,
`os.system("ffprobe ...")`, `shutil.which("ffmpeg")`): card.py proposes their conda-forge
packages as `system` (a customer, 2026-11-20: the image built, then a job failed at run time).
Through a helper too (a customer, 2026-11-21: `_run([_tool("ffmpeg"), ...])` with
`def _tool(name): return shutil.which(name)` proposed none): a function whose argument
reaches a program start (`helpers`, across the modules read) names a program in what it is
called with; and the name of a program card.py knows (`known`), alone in a string given to
any call or first in a list given to one, is one (a word inside a longer string is not).

Other languages: the `amsyos` module's and `amsyos-job`'s calls, by their patterns, and
the known programs they start (`spawn("ffmpeg", ...)`, `exec.Command("ffmpeg", ...)`).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}\.([A-Za-z0-9]{1,10})")
NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
CASTS = {"int": "integer", "float": "number", "str": "string", "bool": "boolean"}
KEYS = {"params": "params", "inputs": "inputs"}
# Calls whose first argument is a file's path: a command-line value given to one is a file.
OPENERS = {
    "open": None, "Path": None, "PurePath": None, "exists": None, "isfile": None,
    "getsize": None, "read_text": None, "read_bytes": None, "ZipFile": "archive",
    "read_csv": "sheet", "read_excel": "sheet", "read_table": "sheet",
    "read_parquet": "table", "load_workbook": "sheet", "loadtxt": "sheet",
    "genfromtxt": "sheet", "read_json": "report", "imread": "image", "VideoCapture": "video",
    "AudioSegment": "audio", "PdfReader": "document",
}  # fmt: skip
FILE_WORDS = ("file", "path", "csv", "image", "photo", "video", "audio", "pdf", "xlsx")
# Calls that start a program, by module, its first argument the command (a list, or a line
# a shell reads); `shutil.which` names one the code means to run.
RUNNERS = {
    "subprocess": frozenset({
        "run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
    }),
    "os": frozenset({"system", "popen", "execv", "execvp", "execl", "execlp"}),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
    "shutil": frozenset({"which"}),
}  # fmt: skip
# Calls that end a job: a test of a file input whose branch ends with one makes it required.
ENDS_JOB = frozenset({"fail", "exit", "_exit", "abort"})
# Calls whose string arguments name something else than a program (a flag, an attribute, a
# param, a file input), so never a known program's name; and those whose first argument is
# a key (`p.get("convert")`: a param named convert).
NAMES_NO_PROGRAM = frozenset({
    "add_argument", "add_parser", "getattr", "hasattr", "setattr", "delattr", "input",
    "input_name", "output", "param", "startswith", "endswith", "split", "rsplit", "replace",
    "strip", "removeprefix", "removesuffix", "index", "count", "find",
})  # fmt: skip
KEYED = frozenset({"get", "pop", "setdefault"})
# The keywords whose value names a program (`format="flac"`, `backend="sox"` do not).
PROGRAM_KEYWORDS = frozenset({
    "executable", "program", "binary", "exe", "cmd", "command", "tool", "args",
})  # fmt: skip
# What a call that makes a copy of a dict copies: `dict(D)`, `D.copy()`, `deepcopy(D)`.
COPIES = frozenset({"dict", "copy", "deepcopy"})
_PROGRAM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}")
# Other languages: a program started by its name (Node's spawn and exec, Go's exec.Command,
# Java's ProcessBuilder and Runtime.exec, R's system and system2).
_RUN_CALL = re.compile(
    r"""\b(?:spawn|spawnSync|execFile|execFileSync|exec|execSync|execa|Command|ProcessBuilder"""
    r"""|system2?)\s*\(\s*(?:\[\s*|c\(\s*|new\s+String\[\]\s*\{\s*)?["'`]([^"'`\s]+)"""
)


# What a module hands to a function, by the function's name: {slot: what}, a slot a
# position (from 0) or a keyword's name, what `params`, `inputs`, `job` or `input:<name>`
# (one file input's value); and `**params` under the slot "**" as the number of positional
# arguments before it.
Passes = dict[str, dict[int | str, str]]
SPLAT = "**"
INPUT = "input:"
# The functions that start a program with what they are given, by name: the slots (a
# position from 0, a keyword's name) whose value names it (`def _tool(name): return
# shutil.which(name)` → {"_tool": {0, "name"}}).
Helpers = dict[str, set[int | str]]


def type_word(value: Any) -> str | None:
    """The card's word for a literal's type (a bool before an int: True is an int)."""
    for kind, word in ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string")):
        if isinstance(value, kind):
            return word
    return None


_JS_PARAM = re.compile(r"""job\.params\.(?!(?:get|items|keys|values|copy)\b)([a-z][a-z0-9_]*)""")
_JS_PARAM_KEY = re.compile(r"""job\.params\[\s*["']([a-z][a-z0-9_]*)["']\s*\]""")
_INPUT_CALL = re.compile(r"""job\.input(?:_name)?\(\s*["']([a-z][a-z0-9_]*)["']""")
_OUTPUT_CALL = re.compile(
    r"""job\.output\(\s*["']([a-z][a-z0-9_]*)["']\s*,\s*["']\.?([a-z0-9]{1,10})["']"""
)
_SH_PARAM = re.compile(r"""amsyos-job["',\s]+param["',\s]+([a-z][a-z0-9_]*)""")
_SH_INPUT = re.compile(r"""amsyos-job["',\s]+input["',\s]+([a-z][a-z0-9_]*)""")
_SH_OUTPUT = re.compile(
    r"""amsyos-job["',\s]+output["',\s]+([a-z][a-z0-9_]*)["',\s]+\.?([a-z0-9]{1,10})"""
)
_RAW_OUTPUT = re.compile(r"""["'](?:\./)?out/([A-Za-z0-9_][A-Za-z0-9_.-]*)\.([a-z0-9]{1,10})["']""")
_RAW_INPUT = re.compile(r"""\[\s*["']inputs["']\s*\]\s*\[\s*["']([a-z][a-z0-9_]*)["']""")


@dataclass
class Found:
    params: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    files: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    outputs: dict[str, str] = field(default_factory=dict[str, str])
    how: set[str] = field(default_factory=set[str])  # sdk, job.json, argparse, amsyos-job
    lines: dict[str, int] = field(default_factory=dict[str, int])  # where each is first read
    base: int = 0  # added to a line: the file's place among those read (card.py `read_code`)
    # The programs the code runs, as written (`ffmpeg`, `./blender/blender`) → the files
    # that run them; `module` is the file being read (card.py `read_code`).
    commands: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    module: str = ""

    def command(self, raw: str) -> None:
        word = raw.strip().strip("\"'")
        name = re.split(r"[\\/]", word)[-1]
        if not _PROGRAM.fullmatch(name):
            return
        where = self.commands.setdefault(word, [])
        if self.module and self.module not in where:
            where.append(self.module)

    def param(self, name: str, line: int | None = None, **facts: Any) -> None:
        if not NAME.fullmatch(name) or name in self.files:
            return
        entry = self.params.setdefault(name, {})
        for key, value in facts.items():
            # The first fact said wins; a default of empty text gives way to a real one.
            if value is not None and (
                key not in entry or (key == "default" and entry[key] == "" and value != "")
            ):
                entry[key] = value
        if line is not None:
            self.lines[name] = min(line + self.base, self.lines.get(name, line + self.base))

    def file(self, name: str, line: int | None = None, **facts: Any) -> None:
        if NAME.fullmatch(name):
            entry = self.files.setdefault(name, {})
            for key, value in facts.items():
                if value is not None and key not in entry:
                    entry[key] = value
            self.params.pop(name, None)
            if line is not None:
                self.lines[name] = min(line + self.base, self.lines.get(name, line + self.base))

    def ordered(self) -> tuple[list[str], list[str]]:
        """The files and the params, in the order the code first reads them."""

        def at(name: str) -> int:
            return self.lines.get(name, 1 << 30)

        return sorted(self.files, key=at), sorted(self.params, key=at)

    def output(self, stem: str, fmt: str) -> None:
        name = re.sub(r"[^a-z0-9_]", "_", stem.lower()).strip("_")
        if not name or not name[0].isalpha():
            name = f"out_{name}" if name else "result"
        self.outputs.setdefault(name[:48], fmt.lower())


# ---------------------------------------------------------------- other languages
def by_patterns(text: str, found: Found) -> None:
    for name in (*_INPUT_CALL.findall(text), *_SH_INPUT.findall(text), *_RAW_INPUT.findall(text)):
        found.file(name)
    for name in (*_JS_PARAM.findall(text), *_JS_PARAM_KEY.findall(text)):
        found.param(name)
        found.how.add("sdk")
    for name in _SH_PARAM.findall(text):
        found.param(name)
        found.how.add("amsyos-job")
    for stem, fmt in (*_OUTPUT_CALL.findall(text), *_SH_OUTPUT.findall(text)):
        found.output(stem, fmt)
    for stem, fmt in _RAW_OUTPUT.findall(text):
        found.output(stem, fmt)


def programs_by_patterns(
    text: str, found: Found, known: Collection[str], *, shell: bool = False
) -> None:
    """The `known` programs code in another language starts (`spawn("ffmpeg", ...)`), and,
    in a shell script, its lines that start with one (`ffmpeg -i ...`)."""
    for raw in _RUN_CALL.findall(text):
        if re.split(r"[\\/]", raw)[-1].lower().removesuffix(".exe") in known:
            found.command(raw)
    for line in text.splitlines() if shell else ():
        words = line.strip().removeprefix("exec ").split()
        if words and re.split(r"[\\/]", words[0])[-1] in known:
            found.command(words[0])


# ---------------------------------------------------------------- Python
def python(
    text: str,
    found: Found,
    passes: Passes | None = None,
    *,
    known: Collection[str] = (),
    helpers: Helpers | None = None,
) -> bool:
    """Read one Python file into `found`, with what other modules hand its functions
    (`passes`), the programs card.py knows (`known`) and the functions of the modules read
    that start one (`helpers`); False when it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    _Reader(tree, found, passes, known=known, helpers=helpers).read()
    return True


def helpers(text: str, known: Helpers | None = None) -> Helpers:
    """This module's functions that start a program with what they are given, read with
    those of the modules read before (`known`: `from util import run` calls one)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    reader = _Reader(tree, Found(), helpers=known)
    reader.find_helpers()
    return reader.helpers


def passes(text: str, known: Passes | None = None) -> Passes:
    """What this module hands to functions by name (`edit(img, job.params)`), read with
    what earlier modules hand its own functions (`known`): a chain of two modules holds."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    return _Reader(tree, Found(), known).handed()


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _choices(elts: list[ast.expr]) -> list[Any] | None:
    """The values of a literal list or tuple, when all are text or all numbers (a card's
    enum: `[0, 90, 180, 270]` stays numbers, a customer's feedback); None otherwise."""
    values: list[Any] = []
    for elt in elts:
        if not isinstance(elt, ast.Constant) or isinstance(elt.value, bool):
            return None
        values.append(elt.value)
    if values and all(isinstance(v, str) for v in values):
        return values
    if values and all(isinstance(v, int | float) for v in values):
        return values
    return None


def _group(node: ast.AST | None) -> list[Any] | None:
    """The values a literal group holds, for an enum: a tuple, list or set (`("1:1",
    "4:5")`), a dict's keys, or `frozenset(...)`, `set(...)`, `tuple(...)`, `list(...)` of
    one; None otherwise."""
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return _choices(node.elts)
    if isinstance(node, ast.Dict):
        keys = [k for k in node.keys if k is not None]
        return _choices(keys) if len(keys) == len(node.keys) else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set", "tuple", "list"}
        and len(node.args) == 1
        and not node.keywords
    ):
        return _group(node.args[0])
    return None


def _literal(node: ast.AST | None) -> tuple[bool, Any]:
    if node is None:
        return False, None
    try:
        value: Any = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return False, None
    if isinstance(value, bool | int | float | str):
        return True, value
    return False, None


def _argv_index(node: ast.Subscript) -> int | None:
    """N of `sys.argv[N]` (or `argv[N]` after `from sys import argv`), N from 1."""
    base = node.value
    is_argv = (
        isinstance(base, ast.Attribute)
        and base.attr == "argv"
        and isinstance(base.value, ast.Name)
        and base.value.id == "sys"
    ) or (isinstance(base, ast.Name) and base.id == "argv")
    index = node.slice
    if not is_argv or not isinstance(node.ctx, ast.Load) or not isinstance(index, ast.Constant):
        return None
    value = index.value
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _opener(call: ast.AST | None, arg: ast.AST) -> tuple[bool, str | None]:
    """Whether `call` opens `arg` (its first argument) as a file, and what that file is when
    the opener says (`read_csv`: a sheet; `Image.open`: an image)."""
    if not isinstance(call, ast.Call) or not call.args or call.args[0] is not arg:
        return False, None
    func = call.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else ""
    )
    if name not in OPENERS:
        return False, None
    if name == "open" and isinstance(func, ast.Attribute):
        owner = func.value
        return True, "image" if isinstance(owner, ast.Name) and owner.id == "Image" else None
    return True, OPENERS[name]


def _mentions_out(node: ast.AST) -> bool:
    """An expression that names the output folder: `out`, `OUT_DIR`, `job / "out"`."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and "out" in sub.id.lower() and sub.id != "stdout":
            return True
        if isinstance(sub, ast.Attribute) and "out" in sub.attr.lower() and sub.attr != "stdout":
            return True
        if _const_str(sub) in {"out", "./out", "out/"}:
            return True
    return False


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _ends_job(body: list[ast.stmt]) -> bool:
    """A branch that ends the job: `raise`, `job.fail(...)`, `sys.exit(...)`."""
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return True
        value = stmt.value if isinstance(stmt, ast.Expr | ast.Return) else None
        if isinstance(value, ast.Call) and _callee(value) in ENDS_JOB:
            return True
    return False


class _Reader:
    def __init__(
        self,
        tree: ast.Module,
        found: Found,
        passes: Passes | None = None,
        *,
        known: Collection[str] = (),
        helpers: Helpers | None = None,
    ) -> None:
        self.tree = tree
        self.found = found
        self.known = frozenset(known)  # programs card.py knows (ffmpeg, blender, ...)
        self.helpers: Helpers = {k: set(v) for k, v in (helpers or {}).items()}
        # Module-level dict literals of text keys (`DEFAULTS = {"crop": None, ...}`), and
        # the names that hold the params merged into one or more of them (`p`).
        self.tables: dict[str, ast.Dict] = {}
        self.merged: dict[str, list[ast.Dict]] = {}
        # Module-level groups of values (`CROPS = ("1:1", "4:5")`, `FILTERS = {"bw": ...}`):
        # a param tested against one takes them as its enum.
        self.groups: dict[str, list[Any]] = {}
        # What this module's functions return (`def settings(params): return {**DEFAULTS,
        # **params}`: a call of it with the params holds them merged), and those being read.
        self.returns: dict[str, list[ast.expr]] = {}
        self._returning: set[str] = set()
        self.parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
        self.holders: dict[str, str] = {}  # a name bound to job.json's params or inputs
        self.var_of: dict[str, str] = {}  # a name bound to one param → the param
        self.jobs: set[str] = {"job"}  # names of the `amsyos` module's job
        self.splats: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, int]] = []
        self.fields: list[ast.ClassDef] = []  # `Settings(**job.params)`: its fields are params
        self.attrs: dict[str, str] = {}  # `self.params = params`: what `self.<attr>` holds
        self.inputs_of: dict[str, str] = {}  # a name bound to one file input → the input
        self.aliases: dict[str, str] = {}  # `from motion import make_video as render`
        self.modules: dict[str, str] = {}  # `import subprocess as sp`: sp → subprocess
        self.imported: dict[str, tuple[str, str]] = {}  # `from shutil import which`
        self.assigned: dict[str, list[ast.expr]] = {}  # what each name is bound to
        self._imports()
        if passes:
            self._given(passes)

    def _imports(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.returns[node.name] = [
                    s.value
                    for s in ast.walk(node)
                    if isinstance(s, ast.Return) and s.value is not None
                ]
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in RUNNERS:
                        self.modules[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if node.module in RUNNERS and alias.name in RUNNERS[node.module]:
                        self.imported[alias.asname or alias.name] = (node.module, alias.name)
                    elif alias.asname:
                        self.aliases[alias.asname] = alias.name
            elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.assigned.setdefault(target.id, []).append(node.value)
        bound: set[str] = set()
        for stmt in self.tree.body:  # `DEFAULTS = {"crop": None, "max_side": 1600}`
            if not isinstance(stmt, ast.Assign | ast.AnnAssign) or stmt.value is None:
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if isinstance(stmt.value, ast.Dict):
                keys = [k for k in stmt.value.keys if k is not None]
                if keys and all(_const_str(k) for k in keys):
                    for name in names:
                        self.tables[name] = stmt.value
            values = _group(stmt.value)
            for name in names:  # bound twice at the top: which values hold is not read
                if name in bound:
                    self.groups.pop(name, None)
                elif values:
                    self.groups[name] = values
                bound.add(name)

    # -- what other modules hand to this one's functions
    def _given(self, passes: Passes) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                slots = passes.get(node.name)
                if slots:
                    self._give(node, slots)
            elif isinstance(node, ast.ClassDef) and passes.get(node.name):
                # `Motion(job.params)`: its __init__ takes them; `Settings(**job.params)`
                # with no __init__ (a dataclass, a model): each field is a param.
                slots = passes[node.name]
                init = next(
                    (
                        s
                        for s in node.body
                        if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef)
                        and s.name == "__init__"
                    ),
                    None,
                )
                if init is not None:
                    self._give(init, slots)
                elif SPLAT in slots:
                    self.fields.append(node)

    def _give(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, slots: dict[int | str, str]
    ) -> None:
        spec = node.args
        names = [a.arg for a in (*spec.posonlyargs, *spec.args)]
        shift = 1 if names and names[0] in {"self", "cls"} else 0
        defaulted = set(names[len(names) - len(spec.defaults) :])
        defaulted |= {
            a.arg for a, d in zip(spec.kwonlyargs, spec.kw_defaults, strict=True) if d is not None
        }
        for slot, what in slots.items():
            if slot == SPLAT:
                self.splats.append((node, int(what) + shift))
                continue
            if isinstance(slot, int):
                at = slot + shift
                name = names[at] if at < len(names) else None
            else:
                known = names + [a.arg for a in spec.kwonlyargs]
                name = slot if slot in known else None
            if name is None:
                continue
            if what == "job":
                self.jobs.add(name)
            elif what.startswith(INPUT):
                self.inputs_of[name] = what[len(INPUT) :]
                if name in defaulted:  # `def render(image, audio=None)`: it may be left out
                    self.found.file(what[len(INPUT) :], optional=True)
            else:
                self.holders[name] = what

    def _splatted(self) -> None:
        """`edit(img, **job.params)`: each argument of `edit` past those given by position
        is a param, its default its default (none: required); `Settings(**job.params)`:
        each field of the class, so."""
        for cls in self.fields:
            for stmt in cls.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                    continue
                has, value = _literal(stmt.value)
                cast = stmt.annotation
                self.found.param(
                    stmt.target.id, stmt.lineno,
                    type=(CASTS.get(cast.id) if isinstance(cast, ast.Name) else None)
                    or (type_word(value) if has else None),
                    default=value if has else None,
                    required=None if stmt.value is not None else True,
                )  # fmt: skip
                self.found.how.add("sdk")
        for node, skip in self.splats:
            spec = node.args
            positional = [*spec.posonlyargs, *spec.args]
            defaults: list[ast.expr | None] = [None] * (len(positional) - len(spec.defaults))
            defaults += list(spec.defaults)
            pairs = list(zip(positional, defaults, strict=True))[skip:]
            pairs += list(zip(spec.kwonlyargs, spec.kw_defaults, strict=True))
            for arg, default in pairs:
                has, value = _literal(default)
                self.found.param(
                    arg.arg, arg.lineno, type=type_word(value) if has else None,
                    default=value if has else None,
                    required=None if default is not None else True,
                )  # fmt: skip
                self.found.how.add("sdk")

    def handed(self) -> Passes:
        """What this module's calls hand to a function: the params or inputs a name holds
        (or `job.params` itself), the job, or `**` of the params."""
        self._holders()
        told: Passes = {}
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if not name or name in {"get", "dict", "int", "float", "str", "bool"}:
                continue
            name = self.aliases.get(name, name)  # `render` imported as make_video's alias
            slots: dict[int | str, str] = {}
            for i, arg in enumerate(node.args):
                if isinstance(arg, ast.Starred):
                    break
                what = self._handed(arg)
                if what:
                    slots[i] = what
            for kw in node.keywords:
                if kw.arg is None:
                    if self._key_of(kw.value) == "params":
                        slots[SPLAT] = str(len(node.args))
                    continue
                what = self._handed(kw.value)
                if what:
                    slots[kw.arg] = what
            if slots:
                told.setdefault(name, {}).update(slots)
        return told

    def _handed(self, arg: ast.AST) -> str | None:
        if self._is_job(arg):
            return "job"
        file = self._input_of(arg)
        return INPUT + file if file else self._key_of(arg)

    def read(self) -> None:
        self._holders()
        self._defaults()
        self._splatted()
        self.find_helpers()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self._call(node)
                self._runs(node)
                self._named(node)
            elif isinstance(node, ast.Subscript):
                self._subscript(node)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                name = _const_str(node.right)
                match = FILE_NAME.fullmatch(name) if name else None
                if match and name and _mentions_out(node.left):
                    self.found.output(name.rsplit(".", 1)[0], match.group(1))
        self._around()
        self._presence()

    # -- what holds job.json's params and inputs
    def _key_of(self, node: ast.AST | None) -> str | None:
        """`params` or `inputs` when the expression is `x.get("params")`, `x["params"]`,
        `job.params`, `x.get("params") or {}`, `dict(...)` or `.copy()` of one of those,
        `{**DEFAULTS, **job.params}`, `dict(DEFAULTS, **params)`, `DEFAULTS | params`, or
        `self.params` bound to one."""
        if node is None:
            return None
        if isinstance(node, ast.BoolOp) and node.values:
            return self._key_of(node.values[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._key_of(node.right) or self._key_of(node.left)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
                return KEYS.get(_const_str(node.args[0]) or "")
            if isinstance(func, ast.Attribute) and func.attr == "copy" and not node.args:
                return self._key_of(func.value)
            if isinstance(func, ast.Name) and func.id == "dict":
                spread = [k.value for k in node.keywords if k.arg is None]
                first = self._pairs(node.args[0]) if node.args else None
                return first or next(
                    (key for v in [*node.args[:1], *spread] if (key := self._key_of(v))), None
                )
            return self._key_returned(node)
        if isinstance(node, ast.DictComp):  # `{k: v for k, v in params.items() if ...}`
            source = self._source(node.generators, node.key)
            return self._key_of(source) if source is not None else None
        if isinstance(node, ast.Dict):
            spread = [v for k, v in zip(node.keys, node.values, strict=True) if k is None]
            return next((key for v in spread if (key := self._key_of(v))), None)
        if isinstance(node, ast.Subscript):
            return KEYS.get(_const_str(node.slice) or "")
        if isinstance(node, ast.Attribute):
            if node.attr == "params" and self._is_job(node.value):  # the `amsyos` module's
                self.found.how.add("sdk")
                return "params"
            held = self._attr(node)
            return held if held in KEYS else None
        if isinstance(node, ast.Name):
            return self.holders.get(node.id)
        return None

    @staticmethod
    def _source(generators: list[ast.comprehension], key: ast.AST) -> ast.expr | None:
        """The dict a comprehension passes the entries of, whatever it filters, when its key
        is the entry's own: `params` of `k: v for k, v in params.items() if v is not None`
        and of `k: params[k] for k in params` (or `params.keys()`); None otherwise."""
        if len(generators) != 1 or not isinstance(key, ast.Name):
            return None
        loop = generators[0]
        target, holder = loop.target, loop.iter
        method: str | None = None
        if (
            isinstance(holder, ast.Call)
            and isinstance(holder.func, ast.Attribute)
            and not holder.args
            and holder.func.attr in {"items", "keys"}
        ):
            method, holder = holder.func.attr, holder.func.value
        if method == "items":
            pair = target.elts if isinstance(target, ast.Tuple) else []
            first = pair[0] if len(pair) == 2 else None
            ok = isinstance(first, ast.Name) and first.id == key.id
        else:
            ok = isinstance(target, ast.Name) and target.id == key.id
        return holder if ok else None

    def _pairs(self, node: ast.AST | None) -> str | None:
        """`params` or `inputs` when an expression gives a holder's entries as pairs, for
        `dict(...)` or `.update(...)`, whatever it filters: `(k, v) for k, v in
        params.items() if ...`, `[(k, v) for ...]`, `filter(f, params.items())`,
        `params.items()`."""
        if isinstance(node, ast.GeneratorExp | ast.ListComp):
            elt = node.elt
            if isinstance(elt, ast.Tuple) and len(elt.elts) == 2:
                source = self._source(node.generators, elt.elts[0])
                return self._key_of(source) if source is not None else None
            return None
        if isinstance(node, ast.Call) and _callee(node) == "filter" and len(node.args) == 2:
            return self._pairs(node.args[1])
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "items"
            and not node.args
        ):
            return self._key_of(node.func.value)
        return None

    def _attr(self, node: ast.AST | None) -> str | None:
        """What `self.<attr>` holds (`self.params = params` in `__init__`)."""
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"self", "cls"}
        ):
            return self.attrs.get(node.attr)
        return None

    def _is_job(self, node: ast.AST | None) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.jobs
        return self._attr(node) == "job"

    def _input_of(self, node: ast.AST | None) -> str | None:
        """The file input an expression's value is: `job.input("x")`, `inputs.get("x")`,
        `inputs["x"]`, or a name bound to one."""
        if isinstance(node, ast.BoolOp) and node.values:
            return self._input_of(node.values[0])
        if isinstance(node, ast.Name):
            return self.inputs_of.get(node.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            func, name = node.func, _const_str(node.args[0])
            if func.attr == "input" and self._is_job(func.value):
                return name
            if func.attr == "get" and self._key_of(func.value) == "inputs":
                return name
            return None
        if isinstance(node, ast.Subscript) and self._key_of(node.value) == "inputs":
            return _const_str(node.slice)
        return None

    def _holders(self) -> None:
        """Names (and `self.<attr>`) bound to the params, the inputs, the job, or one file
        input; again until nothing new is bound (`opts = self.params` may come first)."""
        for _ in range(4):
            before = (len(self.holders), len(self.attrs), len(self.jobs), len(self.inputs_of))
            for node in ast.walk(self.tree):
                self._updated(node)
                if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                what = "job" if self._is_job(node.value) else self._key_of(node.value)
                file = None if what else self._input_of(node.value)
                for target in targets:
                    if isinstance(target, ast.Name):
                        if what == "job":
                            self.jobs.add(target.id)
                        elif what:
                            self.holders[target.id] = what
                            self.found.how.add("job.json")
                            tables = self._tables_in(node.value) if what == "params" else []
                            if tables:
                                self.merged[target.id] = tables
                        elif file:
                            self.inputs_of[target.id] = file
                    elif (
                        what
                        and isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in {"self", "cls"}
                    ):
                        self.attrs[target.attr] = what
            if before == (len(self.holders), len(self.attrs), len(self.jobs), len(self.inputs_of)):
                break

    # -- the params merged into a dict of defaults
    def _tables_in(self, node: ast.AST | None, depth: int = 0) -> list[ast.Dict]:
        """The module-level dicts of defaults an expression merges or copies:
        `{**DEFAULTS, **params}`, `dict(DEFAULTS, **params)`, `DEFAULTS | params`,
        `dict(DEFAULTS)`, `DEFAULTS.copy()`, `deepcopy(DEFAULTS)`, `DEFAULTS`."""
        if node is None or depth > 4:
            return []
        if isinstance(node, ast.Name):
            table = self.tables.get(node.id)
            return [table] if table is not None else []
        if isinstance(node, ast.BoolOp) and node.values:
            return self._tables_in(node.values[0], depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._tables_in(node.left, depth + 1) + self._tables_in(node.right, depth + 1)
        if isinstance(node, ast.Dict):
            spread = [v for k, v in zip(node.keys, node.values, strict=True) if k is None]
            return [t for v in spread for t in self._tables_in(v, depth + 1)]
        if isinstance(node, ast.DictComp):  # `{k: v for k, v in {**DEFAULTS, **p}.items()}`
            return self._tables_in(self._source(node.generators, node.key), depth + 1)
        if isinstance(node, ast.Call) and _callee(node) in COPIES:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "copy" and not node.args:
                return self._tables_in(func.value, depth + 1)
            spread = [k.value for k in node.keywords if k.arg is None]
            return [t for v in [*node.args[:1], *spread] for t in self._tables_in(v, depth + 1)]
        if isinstance(node, ast.Call):
            return [t for v in self._returned(node) for t in self._tables_in(v, depth + 1)]
        return []

    def _returned(self, call: ast.Call) -> list[ast.expr]:
        """What the function of this module a call names returns (`settings(params)` of
        `def settings(params): return {**DEFAULTS, **params}`)."""
        func = call.func
        return list(self.returns.get(func.id, [])) if isinstance(func, ast.Name) else []

    def _key_returned(self, call: ast.Call) -> str | None:
        """`params` or `inputs` when the function a call names returns one; never into a
        function being read already (one that calls itself)."""
        func = call.func
        name = func.id if isinstance(func, ast.Name) else None
        if not name or name in self._returning or name not in self.returns:
            return None
        self._returning.add(name)
        try:
            return next((key for v in self._returned(call) if (key := self._key_of(v))), None)
        finally:
            self._returning.discard(name)

    def _updated(self, node: ast.AST) -> None:
        """`p = dict(DEFAULTS); p.update(params)` (or `p.update(**params)`): `p` holds the
        params merged into DEFAULTS."""
        call = node.value if isinstance(node, ast.Expr) else None
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "update"
            and isinstance(call.func.value, ast.Name)
        ):
            return
        given = [*call.args[:1], *(k.value for k in call.keywords if k.arg is None)]
        if not any("params" in (self._key_of(v), self._pairs(v)) for v in given):
            return
        name = call.func.value.id
        self.holders.setdefault(name, "params")
        self.found.how.add("job.json")
        tables = [t for v in self.assigned.get(name, []) for t in self._tables_in(v)]
        if tables and name not in self.merged:
            self.merged[name] = tables

    def _defaults(self) -> None:
        """Each key of a dict of defaults the params are merged into is a param with that
        default, typed from it; never required (the code has a value without it)."""
        for tables in self.merged.values():
            for table in tables:
                for key, value in zip(table.keys, table.values, strict=True):
                    name = _const_str(key)
                    if not name or key is None:
                        continue
                    has, default = _literal(value)
                    self.found.param(
                        name, key.lineno, type=type_word(default) if has else None,
                        default=default if has else None, required=False,
                    )  # fmt: skip

    def _defaulted(self, holder: ast.AST, name: str) -> bool:
        """Whether `holder[name]` reads a key the merged dict of defaults has."""
        tables = self.merged.get(holder.id, []) if isinstance(holder, ast.Name) else []
        return any(name in {_const_str(k) for k in t.keys} for t in tables)

    # -- reads of one param or input
    def _call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
            key = self._key_of(func.value)
            name = _const_str(node.args[0])
            if key and name:
                has, default = _literal(node.args[1] if len(node.args) > 1 else None)
                self._read(key, name, node, default if has else None)
        elif isinstance(func, ast.Attribute) and func.attr == "add_argument" and node.args:
            self._argument(node)
        elif isinstance(func, ast.Attribute) and func.attr in {"input", "input_name"}:
            base = func.value
            name = _const_str(node.args[0]) if node.args else None
            if self._is_job(base) and name:
                self.found.file(name, node.lineno)
                self.found.how.add("sdk")
        elif isinstance(func, ast.Attribute) and func.attr == "output" and len(node.args) >= 2:
            name, ext = _const_str(node.args[0]), _const_str(node.args[1])
            if name and ext:
                self.found.output(name, ext.lstrip("."))
        elif isinstance(func, ast.Attribute) and func.attr == "join":
            args = node.args
            last = _const_str(args[-1]) if args else None
            match = FILE_NAME.fullmatch(last) if last else None
            if match and last and any(_mentions_out(a) for a in args[:-1]):
                self.found.output(last.rsplit(".", 1)[0], match.group(1))

    def _subscript(self, node: ast.Subscript) -> None:
        index = _argv_index(node)
        if index is not None:
            self._argv(node, index)
            return
        key = self._key_of(node.value)
        name = _const_str(node.slice)
        if key and name and isinstance(node.ctx, ast.Load):
            # `p["crop"]` of `p = {**DEFAULTS, **params}`: required only when not in DEFAULTS.
            self._read(key, name, node, None, required=not self._defaulted(node.value, name))

    def _read(
        self, key: str, name: str, node: ast.AST, default: Any, *, required: bool = False
    ) -> None:
        line = getattr(node, "lineno", None)
        if key == "inputs":  # `inputs.get("audio")`: the code expects it may be missing
            self.found.file(name, line, optional=True if isinstance(node, ast.Call) else None)
            return
        kind: str | None = None
        up = self.parent.get(node)
        # `x.get("n") or 5`: the default; `int(...)`: the type.
        if isinstance(up, ast.BoolOp) and isinstance(up.op, ast.Or) and len(up.values) > 1:
            has, value = _literal(up.values[-1])
            if has and default is None:
                default = value
            up = self.parent.get(up)
        if isinstance(up, ast.Call) and isinstance(up.func, ast.Name) and up.func.id in CASTS:
            kind = CASTS[up.func.id]
            up = self.parent.get(up)
        # The name it is kept in: `x = str(...).strip()`, `ys = [c for c in ...split(",")]`.
        while up is not None and not isinstance(up, ast.stmt):
            up = self.parent.get(up)
        if isinstance(up, ast.Assign) and len(up.targets) == 1:
            target = up.targets[0]
            if isinstance(target, ast.Name):
                self.var_of.setdefault(target.id, name)
        if default is not None and not kind:
            kind = type_word(default)
        self.found.param(
            name, line, type=kind, default=default, required=True if required else None
        )

    # -- the command line read by hand: `sys.argv[1]`
    def _argv(self, node: ast.Subscript, index: int) -> None:
        name = "input" if index == 1 else f"input_{index}"
        where = f"sys.argv[{index}]"
        up = self.parent.get(node)
        kind = (
            CASTS[up.func.id]
            if isinstance(up, ast.Call) and isinstance(up.func, ast.Name) and up.func.id in CASTS
            else None
        )
        opened, means = _opener(up, node)
        var = self._bound(node)
        if not opened and var:
            opened, means = self._opened(lambda n: isinstance(n, ast.Name) and n.id == var)
        if opened and means is None and self._reads_csv():
            means = "sheet"  # `open(path)` given to csv.reader: a sheet
        named = bool(var) and any(w in str(var).lower() for w in FILE_WORDS)
        line = node.lineno
        if kind is None and (opened or named):
            self.found.file(name, line, means=means, check=where, words=var)
        else:
            self.found.param(name, line, type=kind or "string", required=True, check=where)
        self.found.how.add("command line")

    def _reads_csv(self) -> bool:
        """The code reads a CSV file by the csv module (`csv.reader`, `csv.DictReader`)."""
        return any(
            isinstance(n, ast.Attribute)
            and n.attr in {"reader", "DictReader"}
            and isinstance(n.value, ast.Name)
            and n.value.id == "csv"
            for n in ast.walk(self.tree)
        )

    def _bound(self, node: ast.AST) -> str | None:
        """The name a value is kept in: `path = Path(sys.argv[1])` → path."""
        up = self.parent.get(node)
        while up is not None and not isinstance(up, ast.stmt):
            up = self.parent.get(up)
        if isinstance(up, ast.Assign) and len(up.targets) == 1:
            target = up.targets[0]
            if isinstance(target, ast.Name):
                return target.id
        return None

    def _opened(self, holds: Callable[[ast.AST], bool]) -> tuple[bool, str | None]:
        """Whether a value `holds` matches is opened as a file anywhere: given first to an
        opener (`open(x)`, `pd.read_csv(x)`), or `x.read_text()`, `x.open()`."""
        for sub in ast.walk(self.tree):
            if not isinstance(sub, ast.Call):
                continue
            if sub.args and holds(sub.args[0]):
                opened, means = _opener(sub, sub.args[0])
                if opened:
                    return opened, means
            func = sub.func
            if (
                isinstance(func, ast.Attribute)
                and holds(func.value)
                and func.attr in {"read_text", "read_bytes", "open", "exists", "is_file"}
            ):
                return True, None
        return False, None

    def _argument(self, node: ast.Call) -> None:
        flags = [s for a in node.args if (s := _const_str(a))]
        long = next((f for f in flags if f.startswith("--")), None)
        positional = next((f for f in flags if not f.startswith("-")), None)
        raw = long or positional
        if not raw or raw in {"-h", "--help"}:
            return
        name = raw.lstrip("-").replace("-", "_").lower()
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if positional and not long:
            self._positional(node, _const_str(kw.get("dest")) or name, kw)
            return
        kind: str | None = None
        cast = kw.get("type")
        if isinstance(cast, ast.Name):
            kind = CASTS.get(cast.id)
        action = _const_str(kw.get("action"))
        if action in {"store_true", "store_false"}:
            kind = "boolean"
        has, default = _literal(kw.get("default"))
        if action == "store_true" and not has:
            has, default = True, False
        values: list[Any] | None = None
        choices = kw.get("choices")
        if isinstance(choices, ast.List | ast.Tuple | ast.Set):
            values = _choices(choices.elts)
        required = positional is not None or _literal(kw.get("required")) == (True, True)
        self.found.param(
            name, node.lineno, type=kind or (type_word(default) if has else None),
            default=default if has else None, values=values,
            description=_const_str(kw.get("help")), required=True if required else None,
        )  # fmt: skip
        self.found.how.add("argparse")

    def _positional(self, node: ast.Call, name: str, kw: dict[str, ast.expr]) -> None:
        """An argparse positional argument: a file input when its type opens a file
        (`argparse.FileType`, `Path`), its name or help says a file, or `args.<name>` is
        opened; a str input otherwise. Marked `check`: a job gets no command line."""
        cast = kw.get("type")
        cast_name = (
            cast.id
            if isinstance(cast, ast.Name)
            else cast.func.attr
            if isinstance(cast, ast.Call) and isinstance(cast.func, ast.Attribute)
            else cast.attr
            if isinstance(cast, ast.Attribute)
            else None
        )
        kind = CASTS.get(cast_name or "")
        said = _const_str(kw.get("help"))
        words = f"{name} {said or ''}".lower()
        opened, means = (
            (True, None)
            if cast_name in {"FileType", "Path", "open"}
            else self._opened(lambda n: isinstance(n, ast.Attribute) and n.attr == name)
        )
        if opened and means is None and self._reads_csv():
            means = "sheet"
        where = f"the command-line argument {name}"
        if kind is None and (opened or any(w in words for w in FILE_WORDS)):
            self.found.file(
                name, node.lineno, means=means, check=where, description=said, words=words
            )
        else:
            self.found.param(
                name, node.lineno, type=kind or "string", required=True, check=where,
                description=said,
            )  # fmt: skip
        self.found.how.add("argparse")

    # -- what the code says around a param it holds in a name
    def _around(self) -> None:
        by_var = self.var_of
        for node in ast.walk(self.tree):
            if isinstance(node, ast.If | ast.IfExp | ast.Assert | ast.While):
                for sub in ast.walk(node.test):
                    if (
                        isinstance(sub, ast.UnaryOp)
                        and isinstance(sub.op, ast.Not)
                        and isinstance(sub.operand, ast.Name)
                        and sub.operand.id in by_var
                    ):
                        self.found.param(by_var[sub.operand.id], required=True)
            if isinstance(node, ast.Compare):
                left = node.left
                param = (
                    by_var.get(left.id)
                    if isinstance(left, ast.Name)
                    else _const_str(left.slice)  # `p["fit"] not in ("cover", "contain")`
                    if isinstance(left, ast.Subscript) and self._key_of(left.value) == "params"
                    else _const_str(left.args[0])  # `p.get("fit") not in CROPS`
                    if isinstance(left, ast.Call)
                    and isinstance(left.func, ast.Attribute)
                    and left.func.attr == "get"
                    and left.args
                    and self._key_of(left.func.value) == "params"
                    else None
                )
                if param and len(node.ops) == 1 and isinstance(node.ops[0], ast.In | ast.NotIn):
                    values = self._values_of(node.comparators[0])
                    if values:
                        self.found.param(param, values=values)
            text = _const_str(node)
            up = self.parent.get(node)
            if isinstance(up, ast.JoinedStr) or (
                isinstance(up, ast.BinOp) and isinstance(up.op, ast.Add | ast.Mod)
            ):
                # A piece of an f-string or of a sum: `f"y: at most {n}"`, `"crop: one of " +
                # ", ".join(CROPS)` say too little.
                text = None
            if text and ":" in text:
                head, _, rest = text.partition(":")
                head = head.strip()
                if head in self.found.params and rest.strip():
                    words = rest.strip()
                    self.found.param(head, description=words[0].upper() + words[1:200])

    def _values_of(self, group: ast.AST) -> list[Any] | None:
        """The values a param is tested against: a literal group (`("cover", "contain")`),
        or a module-level one by its name (`CROPS`, `FILTERS`, `FILTERS.keys()`: a dict's
        keys; a customer's feedback)."""
        if (
            isinstance(group, ast.Call)
            and isinstance(group.func, ast.Attribute)
            and group.func.attr == "keys"
            and not group.args
        ):
            group = group.func.value
        if isinstance(group, ast.Name):
            return self.groups.get(group.id)
        return _group(group)

    # -- whether the code runs without a file input
    def _presence(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = self.inputs_of.get(node.id)
            elif isinstance(node, ast.Call):
                name = self._input_of(node)
            else:
                continue
            said = self._missing(node) if name else None
            if name and said:
                self.found.file(name, **{said: True})

    def _missing(self, expr: ast.AST) -> str | None:
        """`optional` when the code tests whether a file input's value `expr` was given and
        goes on without it (`if audio is not None:`, `audio or default`); `needed` when
        that test ends the job (`if image is None: job.fail(...)`); None when untested."""
        test: ast.AST = expr
        absent = False  # whether `test` is true when the file was not given
        up = self.parent.get(expr)
        if isinstance(up, ast.Compare):
            other = up.comparators[0] if up.left is expr else up.left
            if (
                len(up.ops) != 1
                or not isinstance(up.ops[0], ast.Is | ast.IsNot | ast.Eq | ast.NotEq)
                or not (isinstance(other, ast.Constant) and other.value is None)
            ):
                return None
            test, absent = up, isinstance(up.ops[0], ast.Is | ast.Eq)
        outer = self.parent.get(test)
        if isinstance(outer, ast.UnaryOp) and isinstance(outer.op, ast.Not):
            test, absent = outer, not absent
            outer = self.parent.get(test)
        if isinstance(outer, ast.BoolOp):
            # `if not a or not b: fail` and `if a and b: ... else: fail` need each.
            if isinstance(outer.op, ast.Or) != absent:
                return "optional"
            test, outer = outer, self.parent.get(outer)
        if isinstance(outer, ast.If) and outer.test is test:
            return "needed" if _ends_job(outer.body if absent else outer.orelse) else "optional"
        if isinstance(outer, ast.Assert) and outer.test is test:
            return None if absent else "needed"
        if isinstance(outer, ast.IfExp | ast.While) and outer.test is test:
            return "optional"
        return "optional" if test is not expr else None  # `has_audio = audio is not None`

    # -- the programs it runs
    def _commands(self, call: ast.Call) -> list[ast.expr]:
        """What names the program a call starts: the command of `subprocess.run(...)`,
        `os.system(...)`, `shutil.which(...)`, or what a helper is given in its slots."""
        func = call.func
        module: str | None = None
        name: str | None = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module, name = self.modules.get(func.value.id), func.attr
        elif isinstance(func, ast.Name) and func.id in self.imported:
            module, name = self.imported[func.id]
        if module is not None and name in RUNNERS[module]:
            first = call.args[0] if call.args else None
            if first is None:
                first = next((k.value for k in call.keywords if k.arg in {"args", "cmd"}), None)
            return [first] if first is not None else []
        slots = self.helpers.get(_callee(call) or "")
        if not slots:
            return []
        given = [
            a for i, a in enumerate(call.args) if i in slots and not isinstance(a, ast.Starred)
        ]
        return given + [k.value for k in call.keywords if k.arg in slots]

    def _runs(self, call: ast.Call) -> None:
        """A program the call starts (`subprocess.run(["ffmpeg", ...])`, `os.system(...)`),
        looks for on PATH (`shutil.which("ffmpeg")`), or hands a helper (`_tool("ffmpeg")`)."""
        for command in self._commands(call):
            word = self._program(command)
            if word:
                self.found.command(word)

    def _named(self, call: ast.Call) -> None:
        """A program card.py knows, named alone in a string given to any call by position
        or by a keyword that names a program, or first in a list given so
        (`log.info("ffmpeg")` too; `"run ffmpeg"`, `format="flac"` are not); never in a
        call whose strings name something else (`p.get("convert")`, `add_argument`)."""
        callee = _callee(call) or ""
        if not self.known or callee in NAMES_NO_PROGRAM:
            return
        given = list(call.args[1:] if callee in KEYED else call.args)
        given += [k.value for k in call.keywords if k.arg in PROGRAM_KEYWORDS]
        for value in given:
            head: ast.expr | None = value
            if isinstance(value, ast.List | ast.Tuple):
                head = value.elts[0] if value.elts else None
            text = _const_str(head)
            if not text or text != text.strip() or " " in text:
                continue
            if re.split(r"[\\/]", text)[-1].removesuffix(".exe") in self.known:
                self.found.command(text)

    def find_helpers(self) -> None:
        """The functions whose argument reaches a program start (`def _tool(name): return
        shutil.which(name) or name`, `def _run(cmd): subprocess.run(cmd, check=True)`), and
        those that hand one to such a function; again until nothing new is found."""
        defs = [
            n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for _ in range(4):
            before = sum(len(v) for v in self.helpers.values())
            for fn in defs:
                spec = fn.args
                names = [a.arg for a in (*spec.posonlyargs, *spec.args)]
                shift = 1 if names and names[0] in {"self", "cls"} else 0
                taken = set(names[shift:]) | {a.arg for a in spec.kwonlyargs}
                local: dict[str, list[ast.expr]] = {}
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Assign):
                        for target in sub.targets:
                            if isinstance(target, ast.Name) and target.id not in taken:
                                local.setdefault(target.id, []).append(sub.value)
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.Call):
                        continue
                    for command in self._commands(sub):
                        arg = self._head(command, taken, local)
                        if arg is None:
                            continue
                        slots = self.helpers.setdefault(fn.name, set())
                        slots.add(arg)
                        if arg in names:
                            slots.add(names.index(arg) - shift)
            if sum(len(v) for v in self.helpers.values()) == before:
                break

    def _head(
        self,
        node: ast.AST | None,
        taken: set[str],
        local: dict[str, list[ast.expr]],
        depth: int = 0,
    ) -> str | None:
        """The function's argument a command's program comes from, when it does: `cmd` of
        `subprocess.run(cmd)`, `exe` of `[exe, "-i", x]`, `name` of `shutil.which(name)`,
        through the function's own names (`cmd = [exe, ...]`)."""
        if node is None or depth > 6:
            return None
        if isinstance(node, ast.Name):
            if node.id in taken:
                return node.id
            bound = local.get(node.id, [])
            return next((a for v in bound if (a := self._head(v, taken, local, depth + 1))), None)
        if isinstance(node, ast.Starred):
            return self._head(node.value, taken, local, depth + 1)
        if isinstance(node, ast.List | ast.Tuple):
            first = node.elts[0] if node.elts else None
            return self._head(first, taken, local, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._head(node.left, taken, local, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._head(node.right, taken, local, depth + 1)
        if isinstance(node, ast.BoolOp):
            heads = (self._head(v, taken, local, depth + 1) for v in node.values)
            return next((a for a in heads if a), None)
        if isinstance(node, ast.JoinedStr) and node.values:
            first = node.values[0]
            if isinstance(first, ast.FormattedValue):
                return self._head(first.value, taken, local, depth + 1)
            return None
        if isinstance(node, ast.Call):
            callee = _callee(node)
            if callee in {"str", "fspath", "Path", "which", "list", "tuple"} and node.args:
                return self._head(node.args[0], taken, local, depth + 1)
            slots = self.helpers.get(callee or "")
            if slots:  # `[_tool(name), ...]` in a function of its own
                for command in self._commands(node):
                    arg = self._head(command, taken, local, depth + 1)
                    if arg:
                        return arg
        return None

    def _program(self, node: ast.AST | None, depth: int = 0) -> str | None:
        """The program a command names: the first word of a line, the first item of a
        list, what a name is bound to (`FFMPEG = shutil.which("ffmpeg") or "ffmpeg"`)."""
        if node is None or depth > 4:
            return None
        if isinstance(node, ast.Constant):
            words = node.value.split() if isinstance(node.value, str) else []
            return words[0] if words else None
        if isinstance(node, ast.JoinedStr):
            # f"ffmpeg -i {x}", f"{BIN}/ffmpeg -i {x}", f"{FFMPEG} -i {x}"
            text = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "\0"
                for v in node.values
            )
            words = text.split()
            word = words[0] if words else ""
            if word == "\0" and isinstance(node.values[0], ast.FormattedValue):
                return self._program(node.values[0].value, depth + 1)
            word = word.rsplit("\0", 1)[-1].lstrip("/\\")
            return word or None
        if isinstance(node, ast.List | ast.Tuple):
            first = node.elts[0] if node.elts else None
            return None if isinstance(first, ast.Starred) else self._program(first, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):  # ["ffmpeg"] + args
            return self._program(node.left, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):  # Path(BIN) / "ffmpeg"
            return self._program(node.right, depth + 1)
        if isinstance(node, ast.BoolOp):
            return next((p for v in node.values if (p := self._program(v, depth + 1))), None)
        if isinstance(node, ast.Call):
            callee = _callee(node)
            if callee == "get" and len(node.args) > 1:  # os.environ.get("FFMPEG", "ffmpeg")
                return self._program(node.args[-1], depth + 1)
            if callee in {"str", "fspath", "split", "which", "Path"} and node.args:
                return self._program(node.args[0], depth + 1)
            if self.helpers.get(callee or ""):  # `[_tool("ffmpeg"), "-i", x]`
                found = (self._program(c, depth + 1) for c in self._commands(node))
                return next((p for p in found if p), None)
            return None
        if isinstance(node, ast.Name):
            bound = self.assigned.get(node.id, [])
            return next((p for v in bound if (p := self._program(v, depth + 1))), None)
        return None
