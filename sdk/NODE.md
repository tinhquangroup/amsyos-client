# AMSYOS Node, protocol 1 (node 2.1)

How the node program (`node/`, Rust) and the Gateway talk (spec v2 §26). The kernel's side is
`kernel/nodes.py` (models, table, join codes), `gateway/node.py` (routes) and
`drivers/node.py` (placing jobs). JSON everywhere; every message is one WebSocket text frame
of at most 64 KiB. Nothing ever dials into the node. Node 2.1 adds the folders its person
allowed, read in place (§6, spec v3 §8.3); a 2.0 node says none, and gets no such job.

## 1. Joining

A manager of the organisation calls the service `node.join_code` (through `amsyos.run`) and
gets a code like `K7QD-M2XP-9HFA`: used once, expires within the hour. The person at the
machine runs `amsyos-node join <code> --gateway https://<host> [--server] [--name <name>]`.

The node makes an Ed25519 key pair on the machine, keeps the secret key in its own folder
(never sent anywhere) and sends:

```
POST {gateway}/node/join
{"code": "K7QD-M2XP-9HFA", "key": "<base64url, no padding, of the 32-byte public key>",
 "kind": "workstation" | "server", "name": "<at most 60 characters>", "machine": Machine}
```

`200 {"node": "n_…", "org": "t_…", "kind": "workstation", "ws": "wss://{host}/node/ws"}`,
or `4xx {"error": CODE, "hint": "what to do"}`. A code is used up only by a join that
succeeds.

`Machine` (bounded on the VPS; anything longer is cut):

```
{"os": "windows" | "linux", "arch": "x86_64", "hostname": "PC2", "version": "2.0.0",
 "cpus": 8, "ram_gb": 31.9, "disk_free_gb": 210.4,
 "gpus": [{"name": "NVIDIA GeForce RTX 3060", "vram_gb": 12.0}],
 "software": {"blender": "4.2.1", "ffmpeg": "7.0.1", "ollama": "0.3.12", "python": "3.12.4"},
 "elevated": false}
```

## 2. The connection

`GET {gateway}/node/ws`, a WebSocket over HTTPS (port 443 through Caddy). The node dials out
and keeps the connection open; when it drops, it dials again after 1, 2, 4 … 60 s (with
jitter).

1. Gateway → `{"t": "challenge", "nonce": "<base64url of 32 random bytes>"}`
2. Node → within 10 s:
   ```
   {"t": "hello", "node": "n_…", "sig": "<base64url Ed25519 signature>",
    "machine": Machine, "services": ["t_amsyos.node.hello", …], "running": ["j_…", …],
    "slots": 1, "idle_s": 412, "load": 0.12, "folders": ["docs", …]}
   ```
   `sig` signs the UTF-8 bytes of `"amsyos-node-hello\n" + node + "\n" + nonce`.
3. Gateway → `{"t": "welcome", "node": "n_…", "org": "t_…", "kind": "workstation",
   "beat_s": 30}`, or `{"t": "refused", "error": CODE, "hint": "…"}` and a close:
   4401 (unknown node or bad signature: join again), 4403 (the node was removed: join
   again), 4409 (a newer connection of the same node took over: stop this one).
4. Node → every 30 s: `{"t": "beat", "services": [...], "running": [...], "slots": 1,
   "idle_s": 412, "load": 0.12}`. A node unheard for 90 s is offline.
   - `services`: the service ids switched on at the machine (§4). The kernel places a job of
     a service only on a node that lists it.
   - `slots`: how many jobs it runs at once (a workstation 1, a server about half its CPUs;
     the person may set it).
   - `idle_s`: seconds since the person last used keyboard or mouse; `null` on a server or
     where it cannot be read. A workstation takes a job that needs or may use a GPU only
     when `idle_s` ≥ 300.
   - `load`: the machine's CPU use, 0 to 1.
   - `folders` (2.1): the names of the folders its person allowed (§6), at most 32, each
     `^[a-z][a-z0-9_-]{0,39}$`; never a path. Anything else in the list is left out.

### Jobs

Gateway → node:

```
{"t": "job", "job": "j_…", "service": "t_amsyos.node.hello", "version": "1.0.0",
 "params": {...},
 "inputs": {"<name>": {"url": "https://…/node/in/<token>", "file": "in/<name>.<ext>"}},
 "folders": {"<name>": {"name": "docs", "path": "reports/2026"}},
 "outputs": ["<name>", …],
 "put": "https://…/upload/node/<token>/",
 "limits": {"cpu": 1, "ram_gb": 1.0, "gpu": "none" | "optional" | "required",
            "vram_gb": null, "max_runtime_s": 3600, "max_output_mb": 200}}
```

The node checks everything itself: the service is on its own list (else it answers
`refused`), `file` matches `^in/[a-z][a-z0-9_]{0,63}(\.[a-z0-9]{1,11})?$`, output names
match `^[a-z][a-z0-9_]{0,63}$`. `folders` (2.1, only for an input of `type: folder`): each
name must be allowed at the machine (else `refused`) and each path is checked as §6 says.

Node → Gateway, for a job it was sent:

| Message | When |
|---|---|
| `{"t": "state", "job": "j_…", "status": "running"}` | at once: the job is taken |
| `{"t": "state", "job": "j_…", "status": "running", "progress": 40}` | the `progress` file changed (at most every 2 s) |
| `{"t": "state", "job": "j_…", "status": "refused", "hint": "…"}` | not started: the service is not switched on, no free slot, or a GPU job while the person uses the machine. Nothing ran; the kernel places it again |
| `{"t": "state", "job": "j_…", "status": "succeeded", "outputs": {"<name>": "<name>.<ext>"}, "took_s": 1.4}` | after every output file was put (below) |
| `{"t": "state", "job": "j_…", "status": "failed", "error": "INVALID_PARAMS" \| "SERVICE_ERROR" \| "TIMEOUT", "hint": "…", "said": "<the end of stderr, at most 2000 characters>"}` | the run failed. Without `error.json`, the hint names the exit code and the line of stderr that says why (the last one naming an error, else the last one; stdout when stderr is empty). The kernel keeps `said` on the job: `amsyos.status` shows it |
| `{"t": "state", "job": "j_…", "status": "cancelled"}` | after a cancel |

Gateway → node: `{"t": "cancel", "job": "j_…"}`. The node stops the job's whole process
tree and answers `cancelled` (or nothing, if it never had the job).

`took_s` is the node's own measure and prices nothing: the kernel trusts no number a node
sends. Sizes and counts are bounded on the VPS.

## 3. Files (2.0: through the Asset Store)

- **In:** `GET` each input's `url` (a short-lived signed link, about 15 minutes) and write
  the body to `file` in the job's folder.
- **Out:** for each `out/<name>.<ext>` whose `<name>` is one of `outputs`, `PUT
  {put}<name>.<ext>` with the file as the body (`Content-Length` set). `200 {"ok": true}`;
  each name once; at most `max_output_mb` each. Then the `succeeded` message names them. A
  file whose name is no output is left out (and said in `said`).

The links are signed by the kernel for this job and this node only, and expire.

## 4. On the machine

- **Switching a service on** is done only by the person at the machine:
  `amsyos-node enable <service id> --dir <folder> --command "<command line>" [--as-job]`,
  `amsyos-node disable <service id>`, `amsyos-node services` (what is on, and what the machine
  could serve: the software found). The list lives on the machine. The kernel sends params
  and file links only: never code, never a command to install anything.
- **A job** is contract 1 (`sdk/CONTRACT.md` §3): its folder holds `job.json`
  (`{"job", "service": {"id", "version"}, "version", "params", "inputs": {"<name>":
  "in/<file>"}}`; a node before 2026-11-23 wrote `service` as the id alone), `in/`,
  `out/`, optional `progress` and `error.json`; `$AMSYOS_JOB` names the folder; the command
  runs in the service's folder. The node program is also `amsyos-job`
  (`amsyos-node job <params|param|input|service|service-version|output|progress|fail> …`,
  and a copy named `amsyos-job` on the job's `PATH`).
- **The folders above a job.** A program that walks its own path (Node.js resolving its
  script, PowerShell setting its start location) reads the attributes of every folder above
  it. On Windows `setup` lets the job account pass through, and read the attributes of, the
  node's folders above a job or a deployed service (`%ProgramData%\AMSYOS\Node`, the jobs
  folder, the managed services folder): `(S,X,RA)` on each folder itself only, never listing
  it, reading what is in it or writing. Run `setup` again after installing a new version of
  the program. A folder switched on with `enable --dir` needs the same of the folders above
  it; on Linux execute (`x`) on them is enough.
- **Isolation.** The node never runs as SYSTEM or root (it refuses to start so). Each job
  runs as the low-privilege account `amsyos-job` (made once by `amsyos-node setup`, as an
  administrator), sees only its own job folder, and is held to its `limits`: a Job Object on
  Windows (memory, CPU rate, killed with the node), cgroups or rlimits on Linux. What the
  node keeps of a job (process id, exit code, log) is in the node's own folder, out of the
  job's reach. A workstation runs jobs at low priority.
- **Where things are.** Windows: the node's folder `%LOCALAPPDATA%\AMSYOS\Node` (key,
  `config.json`, `services.json`, logs), jobs under `%ProgramData%\AMSYOS\Node\jobs` (no
  access for other users), `amsyos-job.exe` under `%ProgramData%\AMSYOS\Node\bin`. Linux:
  `~/.local/share/amsyos-node`, jobs under `/var/lib/amsyos-node/jobs`.

## 5. Access levels (spec v2 §26; additions to protocol 1)

A node has one level: `services` (the default), `managed` or `full`. It lives on the machine
(`config.json`). **Only the person at the machine raises it:** `amsyos-node access
managed|full`; `full` needs an administrator there (Windows: an elevated window; Linux:
`sudo`). Lowering is allowed at the machine and from afar. The node says its level in every
`hello` and `beat` (`"access": "services"`); the kernel believes it only to refuse more, never
to allow more than the node says.

| Level | From afar, besides calling switched-on services | Jobs run as |
|---|---|---|
| `services` | nothing | `amsyos-job` |
| `managed` | `deploy`, `switch` a service (managers) | `amsyos-job` |
| `full` | also `node.exec` and services deployed `as_node` (admins) | `node.exec` and `as_node` services: the node's own account; the rest `amsyos-job` |

### The default `as_node` (node 2.0.2)

At `full` the person at the machine may also make the node's own account the default
(`config.json`: `"as_node_default": true`). Like the level, it is set only there, by an
administrator: `amsyos-node access full --as-node-default`, or `amsyos-node access
as-node-default on` at a node already at `full`. `amsyos-node access as-node-default off`
needs nothing. Any level below `full`, set at the machine or from afar (`lower`), turns it off;
raising `full` again leaves it off. It is never set from afar. The node says it in every
`hello` and `beat` (`"as_node_default": true`; always `false` below `full`), and the kernel
shows it in `node.list` and `describe`.

While it is on:

- a service switched on at the machine runs `as_node` unless it was switched on with
  `amsyos-node enable … --as-job` (`services.json`: `"as_job": true`). This follows the
  default as it is at each job: turning it off makes those services run as `amsyos-job` again;
- a `deploy` by an admin of the organisation (or an agent of one) that does not say `as_node`
  is `as_node: true`. The kernel decides it, since only it knows who is an admin, and sends
  `as_node` explicitly; the node stores what was sent. A deploy that says `as_node: false`, and
  any deploy by a manager who is not an admin, runs as `amsyos-job`. A deployed service keeps
  what its deploy said until it is deployed again;
- `node.exec` is unchanged;
- what never changes: only a direct job of the owning organisation runs as the node's own
  account. A job of an order (another organisation's buyer) and a step of a recipe or a
  mission always run as `amsyos-job` (below);
- `amsyos-node try` runs a service as a direct job would.

Each job run `as_node` is logged: its first `running` state carries `"as_node": true`, and the
kernel keeps it in the node's log of actions (`node.list`: `what: as_node`, by whom, the
service and the job); the node writes it to `node.log` with `by` (a direct job's `job` message
carries `"by": "u_…"`). A change of the default at the machine is logged as `what: access`
with `as_node_default`.

### Actions (Gateway → node)

```
{"t": "action", "id": "x_…", "what": "deploy", "by": "u_…",
 "service": "t_org.name.sub", "command": "<command line>", "as_node": false,
 "package": {"url": "https://…/node/in/<token>", "sha256": "<hex>", "size": 1234}}
{"t": "action", "id": "x_…", "what": "switch", "by": "u_…", "service": "…", "on": true}
{"t": "action", "id": "x_…", "what": "lower", "by": "u_…", "access": "services" | "managed"}
```

- **deploy** (level `managed` or `full`; `as_node` only at `full`):
  - the node fetches the package (a zip, at most 200 MB, from the Gateway's own origin) and
    checks its SHA-256;
  - it unpacks it into a new folder under its managed services root. No entry may leave the
    folder, and links and absolute paths are refused;
  - that folder gets the same protected ACL as `enable` gives (the job account RX, never
    write), and the guard check runs on it;
  - it puts the service in `services.json` as switched on (`managed: true`, `by`, the
    package's hash), replacing an earlier deploy of the same service, and then removes the
    earlier folder;
  - the kernel, once the node says `done`: when a version of the service is defined
    (`compute: node`, with an example), that version now runs the new code, so its check
    runs again (`node.deploy` answers with it; `describe` shows how it ended).
- **switch**: turns a service in `services.json` on or off (any service there).
- **lower**: sets the level to the one named only if it is lower; otherwise it refuses.

Node → Gateway, for each action: `{"t": "done", "id": "x_…", "ok": true, "hint": "…",
"access": "<the level now>"}`, sent at once after the action (and the next beat carries the
new `services` and `access`). Every action is written to `node.log` with `by`, `what` and the
service or level.

### `node.exec` (level `full`, the owning organisation's admins)

It comes as a `job` message with `"service": "node.exec"` and an `exec` part:

```
{"t": "job", "job": "j_…", "service": "node.exec", "version": "1.0.0", "params": {...},
 "exec": {"command": "<command line>", "cwd": "<folder>" | null, "by": "u_…"},
 "inputs": {...}, "outputs": ["log", "<name>", …], "put": "…", "limits": {..., "max_runtime_s": <timeout>}}
```

- **Refused** (`refused`) unless the node is at `full`.
- **How it runs:** as the node's own account, `cmd.exe /d /s /c` on Windows, `sh -c` on Linux.
  It runs in `cwd`, or in the job's folder when `cwd` is null. `$AMSYOS_JOB` names the job's
  folder, and the inputs are in `in/` as for any job. It is killed at `max_runtime_s` with its
  whole process tree.
- **Its files:** stdout and stderr together go to `out/log.txt` (at most 1 MB) and are put as
  the output `log`. Any other output is put only if the command wrote it as `out/<name>.<ext>`;
  a missing one is not an error.
- **Its answer:** exit code 0 is `succeeded`. Anything else is `failed` with `SERVICE_ERROR`, a
  hint naming the exit code, and `said`, the end of the output. On both, `exit_code` is in the
  `state` message.
- **Logged:** each `node.exec` is written to `node.log` with `by` and the command.

### A job of an order, or a step

The kernel adds `"direct": true` to a `job` message only for a call the organisation made
itself (not a step of a recipe or a mission, not an order). The node runs an `as_node` service
(deployed `as_node`, or switched on at the machine while the default `as_node` is on) as its
own account only for a direct job at `full`; every other job of it runs as `amsyos-job`. A
`node.exec` that is not direct is refused.

## 6. Folders read in place (node 2.1; spec v3 §8.3)

**At the machine only.** The person at the machine names folders there; nothing from afar
adds, changes or removes one:

```
amsyos-node allow <name> <folder>     the folder, by its real path; <name>: ^[a-z][a-z0-9_-]{0,39}$
amsyos-node disallow <name>
amsyos-node allowed                   names and paths, on this machine only
```

They are kept in `config.json` (`"folders": {"<name>": "<real path>"}`). Refused: a whole
drive or `/`, and a folder that holds or lies in the node's own folder (its key), the jobs
folder, `bin` or the managed services folder. At most 32. The node says the names in every
`hello` and `beat` (§2) and a beat at once when they change; the Gateway never learns a path.
Like the level, `allow` under `sudo` changes the SUDO_USER's own `config.json`.

**A service's input.** A service of `compute: node` (and only such a service: the kernel
refuses a manifest that gives any other an input of `type: folder`) declares
`{"type": "folder"}`; a call gives `folder:<name>/<path>`. `<path>` is relative, `/` between
its parts (empty, or ending in `/`, for a folder); no part is `..`, `.` or empty, holds
`\ : * ? " < > |` or a control character, starts or ends with a space or ends with a dot (a
Windows device name is refused on Windows); at most 32 parts and 1000 characters. Such a job
is never cached (what lies there may change).

**The kernel's checks** (drivers/node.py, `Nodes.may_read`), all of them, before anything is
sent; otherwise the job ends `FORBIDDEN` with a hint, nothing sent:
1. the job is the organisation's own: never a job of an order, nor a step under one;
2. the job's session's grant lists every folder name in `folders` (spec v3 §4.3; the session
   still live); a job without a session: its caller is a manager of the workspace;
3. some node of the organisation allowed each name; the job is then placed only on an online
   node whose last `hello`/`beat` named every one of them (and that serves the service).

**On the node.** It refuses (`refused`) a name not allowed at the machine, checks each path
again as above, and before the job starts, as its own account (the person's, who can read the
folder):
- resolves `<folder>/<path>`: the folder must still be at the real path `allow` found (not
  moved, no link put there since); no step of the path may be a link (a symbolic link or a
  junction); a file must be a plain file whose real path, read from the handle opened, is the
  one walked to;
- copies it into the job's own folder: a file to `in/<input>.<ext>` (`in/<input>` without an
  extension), a folder to `in/<input>/` with what it holds. Each file is opened as above
  (Linux: `O_NOFOLLOW`, then `/proc/self/fd`; Windows: `GetFinalPathNameByHandleW`); a link
  inside the folder, or anything that is neither a file nor a folder, is left out and said in
  the job's `said`. At most 20,000 files, 8 GB and 32 folders deep, or the job fails;
- makes the copy read-only for the job account (Linux: files `0640`, folders `2750`, group
  `amsyos-job`; Windows: its ACL cut from the job folder's to SYSTEM, Administrators and the
  node's user `F`, `amsyos-job` `RX`). On Linux a file's copy lies in `in/` as any input
  does, so the job may remove it from there (never write it); either way it never reaches
  the person's file;
- `job.json`'s `inputs` names it as any input (`"<input>": "in/<input>.<ext>"`).

A path that is not there, a link, or a folder disallowed since: `failed` with
`INVALID_PARAMS` and a hint that names the input and `folder:<name>/<path>`, never a path of
the machine.

**Why a copy, not a read-only bind or a link.** A bind mount needs root or `CAP_SYS_ADMIN` on
Linux (the node never runs as root) and Windows has none for one process. A symbolic link or a
junction in the job's folder would make the job account open the person's folder itself: that
folder would need a mode or an ACL for `amsyos-job`, changed on the person's own files, the job
could follow a link inside it out of it, and what it read could change while it runs. A copy
made by the node needs no privilege and changes no permission of the person's files; the job
sees a snapshot under its own folder and nothing above it. Its cost is the disk and the time
of the copy, bounded above. Nothing of the folder goes through AMSYOS; only the job's outputs
are put, as for any job.

`amsyos-node try <service> --folder <input>=<name>/<path>` reads one the same way.
