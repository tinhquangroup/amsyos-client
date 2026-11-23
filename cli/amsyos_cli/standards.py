"""A card (or a manifest, or `describe`'s answer) in open standards, and back (the owner,
2026-11-21): an MCP tool definition (MCP 2025-11-25 / 2026-07-28 `Tool`), an A2A Agent Card
and its skills (A2A 1.0.0), and the inputs as a JSON Schema 2020-12 object.

The card stays AMSYOS's source of truth: it says how a service runs, is billed and is
checked (resources, image, effect, cache, estimate, example, tests, means, channels), which
neither standard says. These are exports, and `card_inputs` a start for a new card. AMSYOS
still has six MCP tools: an exported tool is called as `amsyos.run {service_id: <its name>,
params}` (`_meta["amsyos/service_id"]`), never as a tool of its own.

Inputs (`input_schema`), by the manifest's types (kernel/manifest.py `InputType`):

    str     string, minLength 1, maxLength (its max_length, else 1000); a single line also
            a pattern without control characters (left out: several lines may come); with
            a `schema` (contract 2: JSON text) contentMediaType application/json and that
            schema as contentSchema; its `example` as examples
    int     integer, minimum / maximum          float  number, minimum / maximum
    bool    boolean
    enum    enum of its values, typed string, integer or number as the values are; one that
            chooses the format of outputs (`format_of`) says them before its description
    asset   string, format uri, a description that says 'asset://…' (no pattern: describe
            stays short, and the kernel checks the reference), contentMediaType where the
            card pins the format (a schema: JSON; means document.pdf, web.page, patch); with
            a `schema`, anyOf that reference or the JSON itself (the kernel takes it inline)
    folder  string, the pattern 'folder:<name>/<path>' (a node's folder)

Each keeps its `default` and `description`; `required` lists the required; no other
property is taken (additionalProperties false: the kernel refuses an unknown param).
Outputs (`output_schema`): an object of one 'asset://…' string per output, its format as
contentMediaType; one whose format an input chooses (the card's `controls`) says its formats
and that input in its description instead, and A2A's output modes list each format's.

Effect (`annotations`, MCP's hints; the kernel caches, repeats and cancels by `effect`):

    pure      readOnlyHint true,  idempotentHint true (false when `cache: false`: a same
              call may make another result), openWorldHint false
    read      readOnlyHint true,  openWorldHint true
    write     readOnlyHint false, destructiveHint false (true when its id or summary says it
              deletes, removes, overwrites…), idempotentHint false, openWorldHint false
    external  readOnlyHint false, destructiveHint true (it acts outside AMSYOS and is not
              undone: MCP's own default), idempotentHint false, openWorldHint true

(MCP: destructive and idempotent mean something only when a tool is not read-only; a
read-only tool leaves them out, and `describe` stays short.) And back (`effect_of`):
read-only in a closed world is pure, read-only read, a closed world write, anything else
external (MCP's defaults: not read-only, an open world).

The kernel says `input_schema` and `annotations` (without a title: describe has the id) in
`describe` from its own copy of this mapping (kernel/standards.py; the kernel imports
nothing of the CLI): tests/test_standards.py keeps the two the same. Standard library only,
so that the open-source CLI carries it.
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, cast

STR_MAX = 1000  # kernel/manifest.py DEFAULT_STR_MAX
LONG_STR_MAX = 12_000  # kernel/manifest.py LONG_STR_MAX
DESCRIPTION_MAX = 200
# kernel/manifest.py: `_CONTROL_RE` (a single line), `FOLDER_NAME_PATTERN`.
SINGLE_LINE = "^[^\\u0000-\\u001f\\u007f-\\u009f]*$"
FOLDER_PATTERN = "^folder:[a-z][a-z0-9_-]{0,39}(/.*)?$"
ASSET_SAYS = "A file: 'asset://…' (asset.upload stores one)"
FOLDER_SAYS = "A file or folder on your organisation's node, 'folder:<name>/<path>'"
FORMAT_SAYS = "The file format of output"  # an enum whose value an output's format is
JSON = "application/json"
EMBEDDED = "urn:amsyos:input:"  # the `$id` of a file's own schema inside the inputs'
A2A_PROTOCOL = "1.0"  # A2A specification 1.0.0: AgentInterface.protocolVersion
A2A_BINDING = "MCP"  # an open-form binding: AMSYOS is reached over MCP, not A2A's own
ID_KEY, EFFECT_KEY, VERSION_KEY = "amsyos/service_id", "amsyos/effect", "amsyos/version"
EFFECTS = ("pure", "read", "write", "external")
# A file's format → its media type. A format not here has none said.
MEDIA = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
    "gif": "image/gif", "tif": "image/tiff", "tiff": "image/tiff", "svg": "image/svg+xml",
    "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
    "mkv": "video/x-matroska", "wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
    "flac": "audio/flac", "m4a": "audio/mp4", "csv": "text/csv",
    "tsv": "text/tab-separated-values", "json": JSON, "txt": "text/plain",
    "text": "text/plain", "id": "text/plain", "md": "text/markdown", "html": "text/html",
    "url": "text/uri-list", "srt": "application/x-subrip", "vtt": "text/vtt",
    "pdf": "application/pdf", "zip": "application/zip", "tar": "application/x-tar",
    "gz": "application/gzip", "tgz": "application/gzip", "glb": "model/gltf-binary",
    "gltf": "model/gltf+json", "obj": "model/obj", "parquet": "application/vnd.apache.parquet",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "patch": "text/x-diff", "diff": "text/x-diff", "yaml": "application/yaml",
    "xml": "application/xml", "bin": "application/octet-stream",
    "file": "application/octet-stream", "npy": "application/x-npy", "npz": "application/x-npz",
}  # fmt: skip
# A file input's `means` that pins one format (services/core/types.yaml).
MEANS_MEDIA = {"document.pdf": "application/pdf", "web.page": "text/html", "patch": "text/x-diff"}
_FORMAT_OF = {media: fmt for fmt, media in reversed(MEDIA.items())}
_MEANS_OF_MEDIA = (
    ("image/", "image"), ("video/", "video"), ("audio/", "audio"), ("model/", "mesh"),
    ("text/csv", "sheet"), ("application/pdf", "document.pdf"), ("text/html", "web.page"),
    ("text/x-diff", "patch"), ("text/plain", "text"), ("application/json", "report"),
    ("application/x-np", "data"),
)  # fmt: skip
_DELETES = re.compile(
    r"\b(delete[sd]?|remove[sd]?|erase[sd]?|purge[sd]?|wipe[sd]?|overwrite[sd]?|"
    r"destroy(s|ed)?|revoke[sd]?|drop(s|ped)?)\b",
    re.IGNORECASE,
)
_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
_SERVICE = re.compile(r"[a-z][a-z0-9_]{0,47}(\.[a-z][a-z0-9_]{0,47}){1,3}")
_TENANT = re.compile(r"^t_[0-9a-z]{1,32}\.")


def _map(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list | tuple) else []


def _num(value: Any) -> Any:
    """A bound as JSON says it: 1 for 1.0 (the manifest keeps min and max as floats)."""
    if isinstance(value, float) and value.is_integer() and abs(value) <= 2**53:
        return int(value)
    return value


def _line(text: object, most: int = DESCRIPTION_MAX) -> str:
    line = " ".join(text.split()) if isinstance(text, str) else ""
    return line if len(line) <= most else line[: most - 1].rstrip() + "…"


# ---------------------------------------------------------------- card → JSON Schema
def media_of(fmt: object) -> str | None:
    """A file format's media type (png → image/png); None for one not known."""
    return MEDIA.get(fmt.lower()) if isinstance(fmt, str) else None


def _embedded(name: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    """A file's own schema as given, inside the inputs' schema: with a `$ref` of its own
    ('#/$defs/…') it becomes a resource of its own (`$id`), so its refs still resolve."""
    given = dict(schema)
    if "$id" not in given and '"$ref": "#' in json.dumps(given):
        given["$id"] = f"{EMBEDDED}{name}"
    return given


def _unembedded(name: str, schema: object) -> dict[str, Any]:
    """The schema as the card gave it: without the `$id` `_embedded` put in."""
    given = dict(_map(schema))
    if given.get("$id") == f"{EMBEDDED}{name}":
        del given["$id"]
    return given


def property_of(name: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    """One input (the manifest's words: type, values, min, max, …) as a JSON Schema."""
    kind = spec.get("type")
    said = _line(spec.get("description")) or None
    schema = _map(spec.get("schema")) or None
    prop: dict[str, Any] = {}
    if kind == "str":
        prop = {"type": "string", "minLength": 1, "maxLength": spec.get("max_length") or STR_MAX}
        if not spec.get("multiline"):
            prop["pattern"] = SINGLE_LINE
        if schema is not None:
            prop |= {"contentMediaType": JSON, "contentSchema": _embedded(name, schema)}
        if isinstance(spec.get("example"), str):
            prop["examples"] = [spec["example"]]
    elif kind in ("int", "float"):
        prop = {"type": "integer" if kind == "int" else "number"}
        if spec.get("min") is not None:
            prop["minimum"] = _num(spec["min"])
        if spec.get("max") is not None:
            prop["maximum"] = _num(spec["max"])
    elif kind == "bool":
        prop = {"type": "boolean"}
    elif kind == "enum":
        values = _list(spec.get("values"))
        typed = (
            "string" if all(isinstance(v, str) for v in values)
            else "integer" if all(isinstance(v, int) for v in values)
            else "number"
        )  # fmt: skip
        prop = {"type": typed, "enum": values}
        chooses = [str(o) for o in _list(spec.get("format_of"))]
        if chooses:  # a customer, 2026-11-23: the format of the file an output is
            says = f"{FORMAT_SAYS}{'s' if len(chooses) > 1 else ''} {', '.join(chooses)}"
            said = _line(f"{says}: {said}" if said else says)
    elif kind == "asset":
        ref: dict[str, Any] = {"type": "string", "format": "uri"}
        means = [str(m) for m in _list(spec.get("means")) or [spec.get("means")] if m]
        media = JSON if schema is not None else next(
            (MEANS_MEDIA[m] for m in means if m in MEANS_MEDIA), None
        )  # fmt: skip
        if media is not None:
            ref["contentMediaType"] = media
        says = ASSET_SAYS + (", or the JSON itself" if schema is not None else "")
        said = _line(f"{says}: {said}" if said else says)
        prop = ref if schema is None else {"anyOf": [ref, _embedded(name, schema)]}
    elif kind == "folder":
        prop = {"type": "string", "pattern": FOLDER_PATTERN}
        said = _line(f"{FOLDER_SAYS}: {said}" if said else FOLDER_SAYS)
    if said:
        prop["description"] = said
    if spec.get("default") is not None:
        prop["default"] = spec["default"]
    return prop


def input_schema(inputs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The inputs (the manifest's words, as `describe` gives them) as one JSON Schema
    2020-12 object: an MCP tool's `inputSchema`."""
    properties = {name: property_of(name, _map(spec)) for name, spec in inputs.items()}
    told: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    required = [name for name, spec in inputs.items() if _map(spec).get("required")]
    if required:
        told["required"] = required
    return told


def _format(out: Any) -> str | None:
    """An output's format: `png`, or `{format: png, …}` (the card's words)."""
    fmt = out if isinstance(out, str) else _map(out).get("format")
    return fmt if isinstance(fmt, str) else None


def _values(spec: Mapping[str, Any]) -> list[str]:
    return [str(v) for v in _list(spec.get("enum") if "enum" in spec else spec.get("values"))]


def chosen_of(card: Mapping[str, Any]) -> dict[str, tuple[str, list[str]]]:
    """Each output whose format an input chooses → that input and the formats it offers: the
    card's `controls` on the output, or the manifest's (and `describe`'s) `format_of` on the
    input."""
    inputs = _map(card.get("inputs"))
    found: dict[str, tuple[str, list[str]]] = {}
    for name, spec in inputs.items():
        for out in _list(_map(spec).get("format_of")):
            found[str(out)] = (name, _values(_map(spec)))
    for name, out in _map(card.get("outputs")).items():
        by = _map(out).get("controls")
        if isinstance(by, str) and by in inputs:
            found[name] = (by, _values(_map(inputs[by])))
    return found


def output_schema(
    outputs: Mapping[str, Any],
    means: Mapping[str, Any] | None = None,
    chosen: Mapping[str, tuple[str, list[str]]] | None = None,
) -> dict[str, Any]:
    """Each output as the 'asset://…' string of the file it is, its format as
    contentMediaType: an MCP tool's `outputSchema`. An output whose format an input chooses
    (`chosen_of`) says the formats it may be and which input chooses, and no media type."""
    properties: dict[str, Any] = {}
    for name, out in outputs.items():
        fmt = _format(out) or "file"
        by, formats = (chosen or {}).get(name, (None, [fmt]))
        kind = (means or {}).get(name) or _map(out).get("means")
        what = f"the {' or '.join(formats) or fmt} file it makes" + (f" ({kind})" if kind else "")
        prop: dict[str, Any] = {
            "type": "string", "format": "uri", "description": f"{what[0].upper()}{what[1:]}, "
            "as an 'asset://…' reference" + (f"; input {by} chooses its format" if by else ""),
        }  # fmt: skip
        media = media_of(fmt)
        if media is not None and by is None:
            prop["contentMediaType"] = media
        properties[name] = prop
    return {"type": "object", "properties": properties, "required": list(outputs)}


def says_deletes(text: str) -> bool:
    """Its id or summary says it deletes (removes, overwrites, revokes…) what it writes."""
    return _DELETES.search(text.replace(".", " ").replace("_", " ")) is not None


def title_of(service_id: str) -> str:
    """A title from a service's id: `t_42.csv.chart` → 'Csv chart'."""
    words = _TENANT.sub("", service_id).replace(".", " ").replace("_", " ").split()
    text = " ".join(words) or service_id
    return text[:1].upper() + text[1:]


def annotations(
    effect: str | None,
    cache: bool | None = None,
    title: str | None = None,
    *,
    deletes: bool = False,
) -> dict[str, Any]:
    """MCP's tool hints for an effect (the module's docstring has the table)."""
    hints: dict[str, Any]
    match effect or "pure":
        case "pure":
            hints = {"readOnlyHint": True, "idempotentHint": cache is not False}
            hints["openWorldHint"] = False
        case "read":  # destructive and idempotent mean nothing for a read-only tool (MCP)
            hints = {"readOnlyHint": True, "openWorldHint": True}
        case "write":
            hints = {"readOnlyHint": False, "destructiveHint": deletes, "idempotentHint": False}
            hints["openWorldHint"] = False
        case _:  # external
            hints = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
            hints["openWorldHint"] = True
    return {"title": title, **hints} if title else hints


# ---------------------------------------------------------------- whole exports
def _service_id(card: Mapping[str, Any]) -> str:
    return str(card.get("id") or card.get("service") or "")


def _params(card: Mapping[str, Any]) -> dict[str, Any] | None:
    """The example call's params: the card's `example`, or describe's {service_id, params}."""
    example = _map(card.get("example"))
    if "params" in example and "service_id" in example:
        return _map(example["params"])
    return example or None


def mcp_tool(card: Mapping[str, Any]) -> dict[str, Any]:
    """An MCP tool definition of one service: the card's spec (the manifest's words: id or
    service, summary, inputs, outputs, means, effect, cache) or `describe`'s answer, whose
    own `input_schema` and `annotations` are taken as the kernel said them."""
    sid = _service_id(card)
    summary = str(card.get("summary") or "")
    title = title_of(sid)
    effect = str(card.get("effect") or "pure")
    hints = _map(card.get("annotations")) or annotations(
        effect, card.get("cache"), deletes=says_deletes(f"{sid} {summary}")
    )
    hints = {"title": title, **{k: v for k, v in hints.items() if k != "title"}}
    meta: dict[str, Any] = {ID_KEY: sid, EFFECT_KEY: effect}
    if card.get("version"):
        meta[VERSION_KEY] = str(card["version"])
    return {
        "name": sid,
        "title": title,
        "description": summary,
        "inputSchema": _map(card.get("input_schema")) or input_schema(_map(card.get("inputs"))),
        "outputSchema": output_schema(
            _map(card.get("outputs")), _map(card.get("means")), chosen_of(card)
        ),
        "annotations": hints,
        "_meta": meta,
    }


def _modes(media: Iterable[str | None], first: Iterable[str] = ()) -> list[str]:
    return list(dict.fromkeys([*first, *(m for m in media if m)]))


def a2a_skill(card: Mapping[str, Any]) -> dict[str, Any]:
    """An A2A AgentSkill of one service (the same card or answer as `mcp_tool`): its id the
    service's, called as `amsyos.run`; its input modes the params (JSON) and the files' media
    types, its output modes the outputs'."""
    sid = _service_id(card)
    tags = [str(t) for t in _list(card.get("tags"))] or _TENANT.sub("", sid).split(".")
    inputs = _map(card.get("inputs"))
    files = [property_of(n, _map(s)) for n, s in inputs.items() if _map(s).get("type") == "asset"]
    file_media = [
        _map(_list(p.get("anyOf"))[0] if "anyOf" in p else p).get("contentMediaType") for p in files
    ]
    chosen = chosen_of(card)
    out_media = [
        media_of(f)
        for n, o in _map(card.get("outputs")).items()
        for f in (chosen[n][1] if n in chosen else [_format(o)])
    ]
    skill: dict[str, Any] = {
        "id": sid,
        "name": title_of(sid),
        "description": str(card.get("summary") or ""),
        "tags": tags,
    }
    params = _params(card)
    if params is not None:
        skill["examples"] = [json.dumps({"service_id": sid, "params": params}, ensure_ascii=False)]
    skill["inputModes"] = _modes(file_media, [JSON])
    skill["outputModes"] = _modes(out_media) or ["application/octet-stream"]
    return skill


def a2a_card(
    name: str,
    url: str,
    skills: Iterable[Mapping[str, Any]],
    *,
    description: str | None = None,
    version: str = "1.0.0",
) -> dict[str, Any]:
    """An A2A Agent Card (A2A 1.0.0) around skills. AMSYOS serves no A2A endpoint: the one
    interface is its MCP Gateway (`url`, protocolBinding MCP, an open-form binding), where
    `amsyos.run {service_id: <skill id>, params}` calls a skill."""
    return {
        "name": name,
        "description": description
        or f"AMSYOS services: each skill is called with amsyos.run over MCP at {url}",
        "supportedInterfaces": [
            {"url": url, "protocolBinding": A2A_BINDING, "protocolVersion": A2A_PROTOCOL}
        ],
        "version": version,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": [JSON],
        "defaultOutputModes": [JSON],
        "skills": [dict(s) for s in skills],
    }


# ---------------------------------------------------------------- JSON Schema → card
def _plain(schema: object) -> dict[str, Any]:
    """A property's schema with "or null" taken off (connectors/tools.py does the same)."""
    prop = _map(schema)
    for key in ("anyOf", "oneOf"):
        options = _list(prop.get(key))
        rest = [_map(o) for o in options if _map(o).get("type") != "null"]
        if options and len(rest) == 1 and len(rest) < len(options):
            return {**{k: v for k, v in prop.items() if k != key}, **rest[0]}
    kinds = prop.get("type")
    if isinstance(kinds, list):
        left = [k for k in cast("list[Any]", kinds) if k != "null"]
        if len(left) == 1:
            return {**prop, "type": left[0]}
    return prop


def _asset_ref(prop: Mapping[str, Any]) -> bool:
    """A string that is a file's reference: the pattern of an 'asset://…', or a uri whose
    description or media type says it is a file."""
    if prop.get("type") != "string":
        return False
    if str(prop.get("pattern", "")).startswith("^asset://"):
        return True
    said = str(prop.get("description", ""))
    return prop.get("format") == "uri" and ("asset://" in said or "contentMediaType" in prop)


def means_of_media(media: object) -> str | None:
    """What a file of a media type is, in the card's `means` words (image/png → image)."""
    if not isinstance(media, str):
        return None
    return next((m for prefix, m in _MEANS_OF_MEDIA if media.startswith(prefix)), None)


def _strip(said: object, prefix: str) -> str | None:
    """A description without the words `input_schema` put before it."""
    text = said if isinstance(said, str) else ""
    for lead in (prefix + ", or the JSON itself", prefix):
        if text.startswith(lead):
            text = text[len(lead) :].removeprefix(":").strip()
            break
    return text or None


def _card_input(name: str, raw: Mapping[str, Any], notes: list[str]) -> dict[str, Any]:
    prop = _plain(raw)
    told: dict[str, Any] = {}
    said: str | None = _line(prop.get("description")) or None
    options = [_map(o) for o in _list(prop.get("anyOf") or prop.get("oneOf"))]
    refs = [o for o in options if _asset_ref(o)]
    kind = prop.get("type")
    if refs and len(options) == 2:  # a file, or its JSON itself
        given = next(o for o in options if o is not refs[0])
        told = {"type": "file", "schema": _unembedded(name, given)}
        said = _strip(said, ASSET_SAYS)
    elif _asset_ref(prop):
        told = {"type": "file"}
        means = means_of_media(prop.get("contentMediaType"))
        if means:
            told["means"] = means
        said = _strip(said, ASSET_SAYS)
    elif "enum" in prop:
        values = _list(prop.get("enum"))
        texts = all(isinstance(v, str) for v in values)
        numbers = all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)
        if values and (texts or numbers):
            told = {"enum": values}
        else:
            notes.append(f"{name}: an enum of mixed or true/false values: kept as JSON text")
            told = {"type": "string", "multiline": True}
    elif kind == "string" and str(prop.get("pattern", "")).startswith("^folder:"):
        notes.append(
            f"{name}: a node's folder ('folder:<name>/<path>'): a card has no folder type, kept "
            "as text"
        )
        told = {"type": "string"}
        said = _strip(said, FOLDER_SAYS)
    elif kind == "string":
        told = {"type": "string"}
        if prop.get("contentMediaType") == JSON and _map(prop.get("contentSchema")):
            told["schema"] = _unembedded(name, prop["contentSchema"])
        most = prop.get("maxLength")
        if isinstance(most, int) and most != STR_MAX:
            if most > LONG_STR_MAX:
                notes.append(f"{name}: maxLength {most} is over {LONG_STR_MAX}: at most that")
            told["max_length"] = max(1, min(most, LONG_STR_MAX))
        pattern = prop.get("pattern")
        if pattern != SINGLE_LINE:
            told["multiline"] = True  # an outside tool's text may hold several lines
            if pattern is not None:
                notes.append(f"{name}: its pattern is not carried (the code checks it)")
        if prop.get("format") not in (None, "uri"):
            notes.append(f"{name}: format {prop['format']} is not carried (the code checks it)")
        if isinstance(prop.get("minLength"), int) and prop["minLength"] > 1:
            notes.append(f"{name}: minLength {prop['minLength']} is not carried")
    elif kind in ("integer", "number"):
        told = {"type": kind}
        for key, word in (("minimum", "min"), ("maximum", "max")):
            bound = prop.get(key)
            if isinstance(bound, int | float) and not isinstance(bound, bool):
                told[word] = bound
        for key in ("exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
            if key in prop:
                notes.append(f"{name}: {key} is not carried (min and max are inclusive)")
    elif kind == "boolean":
        told = {"type": "boolean"}
    else:
        shape = f"JSON {kind}" if kind in ("object", "array") else "JSON"
        notes.append(f"{name}: {shape} goes as JSON text (its code decodes it), its schema kept")
        told = {"type": "string", "multiline": True, "schema": dict(prop)}
        said = _line(f"{shape}, as text: {said}" if said else f"{shape}, as text")
    if said:
        told["description"] = said
    default = prop.get("default")
    if default is not None and not isinstance(default, dict | list):
        told["default"] = default
    examples = _list(prop.get("examples"))
    if examples and told.get("type") == "string":
        first = examples[0]
        told["example"] = first if isinstance(first, str) else json.dumps(first)
    return told


def card_inputs(schema: Mapping[str, Any]) -> dict[str, Any]:
    """An MCP tool's `inputSchema` (or any JSON Schema object) as a card's `inputs`, in the
    card's words (type string|integer|number|boolean|file, enum, min, max, max_length,
    multiline, schema, means, default, description, example), and a note for each thing a
    card cannot carry: {"inputs": {...}, "notes": [...]}."""
    notes: list[str] = []
    if schema.get("type") not in (None, "object"):
        notes.append(f"the schema is of type {schema.get('type')}, not an object: no inputs")
        return {"inputs": {}, "notes": notes}
    required = {str(n) for n in _list(schema.get("required"))}
    inputs: dict[str, Any] = {}
    for name, raw in _map(schema.get("properties")).items():
        if not _NAME.fullmatch(name):
            notes.append(
                f"{name}: not a card's input name (snake_case: a-z, 0-9, _): left out; rename "
                "it in the code and the card"
            )
            continue
        told = _card_input(name, _map(raw), notes)
        if name in required:
            told.pop("default", None)
            told["required"] = True
        inputs[name] = told
    for key in ("patternProperties", "allOf", "oneOf", "anyOf", "if", "dependentRequired"):
        if key in schema:
            notes.append(f"{key} at the top is not carried: each input is checked on its own")
    return {"inputs": inputs, "notes": notes}


def effect_of(tool: Mapping[str, Any]) -> str:
    """An MCP tool's effect: `_meta["amsyos/effect"]` where AMSYOS exported it, else its
    hints (MCP's defaults where unsaid: not read-only, an open world)."""
    said = _map(tool.get("_meta")).get(EFFECT_KEY)
    if said in EFFECTS:
        return str(said)
    hints = _map(tool.get("annotations"))
    read_only = hints.get("readOnlyHint") is True
    open_world = hints.get("openWorldHint") is not False
    if read_only:
        return "read" if open_world else "pure"
    return "external" if open_world else "write"


def outputs_of(schema: Mapping[str, Any]) -> dict[str, Any]:
    """An MCP tool's `outputSchema` as a card's outputs, where a property says a file's
    media type AMSYOS knows ({name: {type: file, format, means}}); nothing else."""
    found: dict[str, Any] = {}
    for name, raw in _map(schema.get("properties")).items():
        fmt = _FORMAT_OF.get(str(_map(raw).get("contentMediaType")))
        if _NAME.fullmatch(name) and fmt is not None:
            out: dict[str, Any] = {"type": "file", "format": fmt}
            means = means_of_media(_map(raw).get("contentMediaType"))
            if means:
                out["means"] = means
            found[name] = out
    return found


def name_of(tool: Mapping[str, Any]) -> str | None:
    """A card's service name from an MCP tool: the one AMSYOS exported, else its name when
    it is one (2 to 4 snake_case words with dots)."""
    for given in (_map(tool.get("_meta")).get(ID_KEY), tool.get("name")):
        name = _TENANT.sub("", given) if isinstance(given, str) else ""
        if _SERVICE.fullmatch(name):
            return name
    return None
