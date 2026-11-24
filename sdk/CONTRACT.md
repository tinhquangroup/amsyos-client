# AMSYOS service contract 1

Every service an organisation adds says the same things the same way, whatever its language or shape. That is what lets AMSYOS find it (`discover`), join it with other services (`mission.plan`), check it and offer it to another organisation (the owner, 2026-11-18; spec v2 §25). A manifest may say `contract: 1`; it is the default.

## 1. The service card

A service is its manifest. A new service of an organisation, or a new version of one, of kind `hosted` (its own code) or `composed` (steps of other services), is refused unless:

- **Every file is typed.** Every file input (`type: asset`) has `means`. Every output has a type in `means`. A type is one of the operator's (services/core/types.yaml) or one of the organisation's own (`t_<org>.<name>`, see §4), and it fits the output's format: an `image.*` output is png, jpg, webp, gif, tif or psd, a `video.*` mp4, mov, webm or mkv, and so on. `text` suits a log or a note, `report` a JSON report.
- **It is said in English.** The summary (at most 120 characters, what it takes and what it makes) and every input's description.
- **It gives an example** (`hosted` only). `example` holds the params of one call; files are `asset://` refs the organisation may read. The example is run once when the version is defined (§5).

Everything else is as it always was: `inputs` (types, defaults, limits), `outputs` (a format each), `effect` (pure, read, write, external), `estimate`.

`service.check {spec}` reports every problem at once before anything is defined. It also says which services the caller may use make each input type and which take each output type: where the planner would put the service. Services registered before contract 1 keep running; `describe` says what they lack.

## 2. Four shapes, one card

| Shape | How it is made | Its files are typed by |
|---|---|---|
| Own code, any language, on the organisation's rented node | `node.build` with `service` (or `service.define` with `image`) | the card: `means` |
| Steps of other services | `service.define` with `steps` | the card: `means` |
| The organisation's own MCP server | `connection.add`, then `connection.types {connection, tool, files, makes}` | `connection.types`: which text arguments are files (the tool gets a short-lived link) and which files it returns |
| A provider's REST API | `provider.add`, then `service.define` with `call` | the card |

The refusals of §1 apply to the first two shapes (`kernel/conform.py`, `bound`): a connector's tools and a service with a `call` are not refused for a missing type, and are typed by `connection.types` and by their card when they are.

## 3. The job contract (own code)

Each call is one run of the image's `command` in its source folder, as an unprivileged user, on its rented node: a machine rented for the organisation alone. `$AMSYOS_JOB` names the job's folder:

| Path | What |
|---|---|
| `job.json` | `{"params": {...}, "inputs": {"<name>": "in/<file>"}, "service": {"id", "version"}}`: params checked, defaults filled in; `service` says which service the job is for (an image several services share tells their jobs apart by it; additive, a job may ignore it) |
| `in/` | the input files |
| `out/<output>.<ext>` | one file an output, as the card declares it; at most 2 GB in all a job: over it the job fails `OUTPUT_TOO_BIG`, naming each file and its size (empty files and files over it are not kept as a failed job's partial files) |
| `progress` | optional, 0 to 100 |
| `error.json` | optional, `{"error": "INVALID_PARAMS" or "SERVICE_ERROR", "hint": "..."}` with exit code 1: what the caller reads to fix its call |

A job may run while the rented node is up (at least one hour; a job past its limit fails `TIMEOUT` with its partial files and its log's end). The machine has a GPU and the Internet; no AMSYOS key is on it.

**Languages.** `node.build` takes:
- `runtime`: python (the default: Python 3.11 with PyTorch), node, java, go or r, installed from pinned conda-forge packages without root;
- `system`: up to 20 more conda-forge programs (ffmpeg, imagemagick…);
- `setup`: one build command run in the source folder (`npm ci`, `go build -o app .`, `mvn -q package`). Without one, `npm ci` runs when there is a package-lock.json.

A static binary shipped in the archive needs no runtime. Exactly what was installed is kept (pip pins and the conda lock), so a new machine makes the same image again.

**Bindings,** always on the machine:

| Language | Use |
|---|---|
| Python | `from amsyos import job`: `job.params`, `job.input(name)`, `job.output(name, ".png")`, `job.progress(40)`, `job.fail(hint)`, `job.service` and `job.service_version` |
| Node | `const { job } = require("amsyos")` (or `import { job } from "amsyos"`): the same surface (`job.serviceVersion`) |
| Any other | the command `amsyos-job`: `params`, `param <name> [--raw]`, `input <name>`, `output <name> [.ext]`, `progress <n>`, `fail <hint> [code]`, `service`, `service-version` |

Following the folder layout by hand is just as good.

**A server kept warm** (optional). `node.build` takes `serve` (e.g. `python server.py`) instead of `command` or beside it. The process loads what it needs once, then does jobs one after the other. The first call is cold and the next ones are warm.
- **Protocol.** One line each way, at most 4,096 bytes. A job comes on stdin as `{"job": "<id>", "folder": "<the job's folder>"}`. The process does it with the folder contract above, then writes `{"done": "<id>"}` on stdout. Anything else on stdout goes to the job's log, so print to stderr in your own loop.
- **Bindings.** Python `from amsyos import serve; serve(handle)`, Node `serve(async (job) => …)`, any other language a loop on `amsyos-job next` … `amsyos-job done`. `job.fail` ends the job, not the server.
- **Who shares a process.** One process serves one service version, one workspace and one paying organisation; two buyers of an offered service never share one.
- **Limits.** At most 2 processes a machine (fewer when the declared `vram_gb` would not fit). The one unused longest is stopped first. A process is renewed after 100 jobs or one hour.
- **When a process stops.** A crash fails its job with the tail of stderr. A cancelled or timed-out job stops its process. Either way the next job starts cold. Processes end when the rented node is given back; idle closing is unchanged, and a waiting process is not activity.
- **After a build.** The check run goes through the server, so it is warm for the first real call.

## 4. Types of the organisation's own

`type.define {name: t_<org>.<name>, kind_of?, words?}` (manager). `kind_of` names an operator type, or another of the organisation's own, that a file of the type can always stand for: `t_42.invoice` kind_of `document.invoice` goes wherever a `document.invoice` is taken, never the other way round. The chain ends at an operator type and has no loop. `words` are what a goal in the organisation's words is read by. An organisation's types and words are its own: another organisation sees only the type ids on a service offered to it, and what operator types they stand for. A type of an organisation is about that organisation (`kernel.types.scope`): a plan for another goal goes through it only when nothing else does.

## 5. The check

When a version of own code is defined, AMSYOS runs its `example` once, as the manager who defined it, in the same workspace, paid like any job. Then it checks that:
- the job succeeded;
- every declared output is there and not empty;
- each file's first bytes are its declared format (images: png, jpg, webp, gif, tif, psd; pdf; video and sound: mp4/mov/m4a, webm/mkv, wav, mp3, ogg, flac; archives and documents: zip, xlsx, docx, pptx, odt, gz/tgz, tar; glb, parquet), JSON (json, gltf, geojson) parses and text (txt, md, csv, srt, vtt, html, obj, bvh) is UTF-8; `kernel/conform.py` holds the list.

Until the check has passed, the organisation may run the version itself, but `mission.plan` does not join it and `offer.make` refuses it. `describe` shows `check: {state, job, at, problems}`. Building the same tag again checks again.

## 6. What changes the contract

A change that a service written for contract 1 would not meet raises the number. Contract 1 stays readable.

## 7. Contract 2: the three parties

Contract 2 (the owner, 2026-11-19; spec v2 §30) is one contract between three parties: the organisation that builds a service, the kernel, and the AI client that calls it. Contract 1 is a part of it: everything above still holds.

| Party | Promises | Is promised |
|---|---|---|
| You, the organisation that builds a service | a true and whole card: every JSON file (in and out) with its schema and a valid example; the provider's terms (resale); an estimate; errors fixable in one turn | the kernel checks inputs for you before a machine is spent; you are told which inputs clients get wrong and which gaps are near your service; you are paid |
| The kernel | shows a client only what it can run; checks every input by the card before running; time and cost from measured runs; joins files by type and schema; isolates, meters, bills | cards that passed their check; clients that send intent |
| The AI client | sends intent and its own content (a script, lines), never code; writes no glue: what is missing it says as a gap; judges and votes on results | never guesses a format; one mistake costs one turn; never writes code to join two services |

**What changes for you now** (step 1, 2026-11-19): say the shape of every JSON file.
- An input of `type: asset` whose file is JSON, or of `type: str` that is JSON text, may carry `schema`: a JSON Schema (draft 2020-12), inline in your spec, at most 20,000 characters, and the whole spec, its schemas included, within its 12,000.
- `schemas` maps a JSON output's name to its schema. `describe` shows it; from step 3 the kernel joins by it.
- `service.check` and `service.define` refuse a schema that is not a valid JSON Schema or is too long, with the other problems at once.
- A schema is not required yet: a card without one keeps working, and `contract: 1` stays the default (§6). But what a card does not say, a client guesses, a run and a turn at a time.

**What the kernel does with it.**
- Before your code runs, and before a machine is rented or woken, the kernel checks a client's text or file against your schema: the text parsed, the file read from the Asset Store (at most 2 MB) and parsed. A step of a mission is checked the same way, also when an earlier step made its file.
- A call that does not fit is refused with every problem at once (up to 20): `{"error": "INVALID_INPUT", "hint": …}`, naming the input, the file and each problem at its JSON path. It is never `INVALID_PARAMS`, so the kernel's model, which proposes fixes for refused params (spec v2 §29), never sees a client's file.
- The check of §5 runs your example through the same admission, so the example must pass your schema.
- `describe` shows a client the shape, not the schema: each key with its type and enums, the required ones marked, in at most about 2,500 characters an input or output.

**A schema is never stricter than your code.** The kernel refuses what your schema refuses before your code can say otherwise. A schema stricter than your code turns away calls that would have worked; a looser one only leaves the refusal to your code, as before. Write it from what your code really checks: say `additionalProperties: false` only if your code refuses keys it does not know. AMSYOS makes its builtins' schemas from the models their jobs validate with (`scripts/schemas.py`). Check that your example and the files you know work pass both.

The card, in YAML for reading (`service.define` takes the same as JSON text; the image, `effect` and `estimate` left out). The same `schema` goes on a `type: asset` input whose file is JSON:

```yaml
summary: "Make a PDF invoice from a JSON list of items; returns the PDF and its totals"
inputs:
  items:
    type: str
    required: true
    multiline: true
    description: "JSON text: the buyer and the items, each a name, a quantity and a unit price"
    schema:
      type: object
      required: [buyer, items]
      properties:
        buyer: {type: string, maxLength: 200}
        currency: {enum: [USD, EUR, VND]}
        items:
          type: array
          minItems: 1
          items:
            type: object
            required: [name, qty, price]
            properties:
              name: {type: string}
              qty: {type: integer, minimum: 1}
              price: {type: number, minimum: 0}
outputs:
  invoice: pdf
  totals: json
means: {invoice: document.invoice, totals: report}
schemas:
  totals:
    type: object
    required: [net, tax, gross]
    properties:
      net: {type: number}
      tax: {type: number}
      gross: {type: number}
example:
  items: '{"buyer": "Acme Ltd", "items": [{"name": "Desk", "qty": 2, "price": 120}]}'
```

### What a client is shown (step 2)

The kernel shows a client only what its organisation can run, from the same checks `run` makes. If your service calls or rents from a provider whose terms do not let AMSYOS sell what it makes to that organisation, `discover` leaves it out for that organisation, and every recipe that always runs it as a step too (a step behind a `when`, or an optional one, does not count). `describe` says why in `cannot_run`. A recipe whose step belongs to an app the workspace has not switched on is shown marked `ready: false`, with what a manager does. `run` of a recipe checks the steps its plan will run before anything is queued, so a client never pays for the first steps of a recipe that a later step makes impossible. Your offers: `describe` tells the buyer, in `cannot_run`, when its workspace has not accepted the offer or your provider's terms refuse the sale.

### Joins (step 3)

Your output's schema is held against the schema of every input it feeds, by type (`means`) and in recipes, missions and composed services: `scripts/joins.py` does it for the builtins. A **break** (the input always refuses what your output is: a type, a required key you never write, a const) means the join is never made, and `service.check` refuses a composed service that has one. A **risk** (a key you may leave out that the input requires, narrower bounds) means it is made, and the file is checked when the next step is let in. Say `means` on every JSON output and file input that another service makes or takes, and the same schema on both sides of a join you intend: that is how a client joins your service to others without code. Where a client would have to edit a file between two services, `asset.patch` does it (a JSON Patch, checked against the input it will feed). A client may give a JSON file input that has a schema inline, as the JSON itself in `amsyos.run`'s params.

### A property your content sets

A property asked of a plan (`mission.plan {constraints}`: size, aspect, duration, format, language) is met only by a service that says it sets it (`controls`). When what the client writes sets it, name that input: `controls: {duration: script, language: script}`. A film's length and language are set by its script. A text input of several lines, or over 64 characters, is content. A plan that ends in your service then meets the constraint without setting a value in it, and tells the client in `fill` that what it writes decides it. This holds only for what it can mean: `duration` of a video or an audio output, `language` of a video, audio, text or subtitles output.

## 8. The card in open standards: MCP, JSON Schema, A2A

The card stays the source of truth (the owner, 2026-11-21): it says how a service runs, is billed and is checked, which no standard says. It exports to the standards and can start from one. AMSYOS still has six MCP tools: an exported tool or skill is called as `amsyos.run {service_id: <its name or id>, params}`, never as a tool of its own. One mapping does it, `cli/amsyos_cli/standards.py` (standard library only); the kernel keeps a copy for `describe` (`kernel/standards.py`, held the same by `tests/test_standards.py`).

**Out.** `describe` carries `input_schema` (the inputs as a JSON Schema 2020-12 object: an MCP tool's `inputSchema`) and `annotations` (MCP's tool hints). `amsyos card --export mcp|a2a [--out file]` turns `amsyos.yaml` into an MCP tool definition or an A2A Agent Card, here, with nothing sent; `amsyos describe <service> --export mcp|a2a` does the same from the kernel's answer.

| Card | JSON Schema (MCP `inputSchema`, `outputSchema`) |
|---|---|
| `str` | `string`, `minLength` 1, `maxLength` (its `max_length`, else 1000); one line: a `pattern` without control characters; with `schema` (JSON text): `contentMediaType: application/json` and the schema as `contentSchema` |
| `int`, `float` | `integer`, `number`, with `minimum` and `maximum` |
| `bool`, `enum` | `boolean`; `enum` typed `string`, `integer` or `number` as its values are |
| `asset` | `string`, `format: uri`, a description that says `asset://…`; `contentMediaType` where the card pins the format; with `schema`: `anyOf` the reference or the JSON itself |
| `folder` | `string` with the pattern `folder:<name>/<path>` |
| every input | `default`, `description`; `required`; `additionalProperties: false` (the kernel refuses an unknown param) |
| outputs | an object of one `asset://…` string each, its format as `contentMediaType` |

| `effect` | MCP `annotations` |
|---|---|
| `pure` | `readOnlyHint` true, `idempotentHint` true (false with `cache: false`), `openWorldHint` false |
| `read` | `readOnlyHint` true, `openWorldHint` true |
| `write` | `readOnlyHint` false, `destructiveHint` false (true when its id or summary says it deletes, removes, overwrites…), `idempotentHint` false, `openWorldHint` false |
| `external` | `readOnlyHint` false, `destructiveHint` true, `idempotentHint` false, `openWorldHint` true |

An MCP export also has `name` (the service id), `title`, `description` (the summary) and `_meta` (`amsyos/service_id`, `amsyos/effect`, `amsyos/version`). An A2A export follows A2A 1.0.0: a skill per service (`id` the service id, `name`, `description` the summary, `tags`, `examples` one whole call as JSON text, `inputModes` application/json and the files' media types, `outputModes` the outputs'), in a card whose one `supportedInterfaces` entry is the Gateway's MCP URL with `protocolBinding: MCP` (an open-form binding: AMSYOS serves no A2A endpoint).

**In.** `amsyos init --from-mcp tool.json` starts a card from an MCP tool definition: its description gives the summary, its `inputSchema` the inputs (string, integer, number, boolean, enum, a file reference, JSON text with its schema; "or null" is dropped, an object or array goes as JSON text that keeps its schema), its `annotations` the effect (read-only in a closed world: pure; read-only: read; a closed world: write; else external, as MCP's defaults say) or `_meta.amsyos/effect` where AMSYOS exported it, its `outputSchema` outputs whose media type AMSYOS knows. What `init` reads from the code (runtime, command, packages, outputs, inputs the tool does not list) stays. Each thing a card cannot carry is noted: a `pattern` or `format` of a string, `exclusiveMinimum`, an enum of mixed values, a name that is not snake_case.

**No counterpart** in either standard, kept in the card alone: resources, image and runtime (`system`, `setup`, `command`, `serve`), what `effect` makes the kernel do beyond the hints (never repeat or cache an action that left AMSYOS), `cache` (only as `idempotentHint`), `estimate`, `example` (A2A: a text), `tests`, `means` (only as a media type where a type pins one), the outputs' `schemas`, `controls`, `pilot`, channels (`dev`, `stable`). A round trip (card → JSON Schema → card) keeps every input's type, bounds, enum, default, description, `max_length`, one line or several, and a JSON text's or file's schema; it loses a file input's `means` (unless its type pins a format), and a folder becomes text. JSON Schema 2020-12 treats `contentSchema` as an annotation: the kernel, not a JSON Schema validator, checks JSON text against it at admission (§7).

