# AMSYOS Node

One program, one file, that makes a computer an organisation owns a place where that
organisation's services run (spec v2 §26). It dials out to the Gateway; nothing dials into
it. Protocol 1, between the node and the Gateway: [sdk/NODE.md](../sdk/NODE.md). The job a
service does is contract 1: [sdk/CONTRACT.md](../sdk/CONTRACT.md) §3.

## Build

No Rust on the VPS: `node/build.sh` builds in Docker (`rust:1.99.0-bookworm`, with mingw and
musl) and writes `node/dist/amsyos-node.exe` (Windows x86_64) and `node/dist/amsyos-node`
(Linux x86_64, static). `node/build.sh linux|windows` builds one; the cargo registry is kept
in the volume `amsyos-node-cargo`. The Python job module, `sdk/amsyos/__init__.py`, is built
into the program (`src/pymod.rs`): `build.sh` copies it to `py/amsyos/` first, since only
`node/` is mounted in Docker.

## On a computer

A manager of the organisation gets a code: the service `node.join_code` (it also prints the
command). Then, at the machine (Windows: a cmd window of an administrator; Linux: `sudo` for
`setup` only):

```
amsyos-node setup [--jobs D:\AMSYOS\jobs]         once: the job account amsyos-job, the jobs folder
amsyos-node join <code> --gateway https://<host> [--server] [--name <name>]
amsyos-node enable <service id> --dir <folder> --command "<command line>" [--serve "<command line>"] [--as-job]
amsyos-node access services|managed|full [--as-node-default]   the level; full needs an administrator
amsyos-node access as-node-default on|off          at full: services run as this account by default
amsyos-node allow <name> <folder>                  2.1: jobs read it in place as folder:<name>/<path>
amsyos-node disallow <name> | allowed              (the Gateway learns the name only)
amsyos-node try <service id> --params '<json>' --input <name>=<file> [--folder <input>=<name>/<path>]
                                                   a job here, before going online
amsyos-node run                                    online; Ctrl+C stops it
amsyos-node info | services | disable <id> | job …
amsyos-node diag [<folder>]                        Windows: why a job does not start as amsyos-job
```

**A new version of the program:** stop the node (its scheduled task, or Ctrl+C), run the new
file's `setup` again in an administrator window with the same `--jobs` (and `--services`) as
before (it copies the program into `%ProgramFiles%\AMSYOS\Node` and sets the folders' ACLs
again), then start the node again. Join, services, the level and its default are kept. On
Linux, run `sudo amsyos-node setup` again the same way: it copies `amsyos-job` and the Python
job module that every job uses (`run` warns while they are not this version's).

**Python jobs (2.1.2).** Every job's PYTHONPATH holds the module `amsyos` (`from amsyos import
job, serve`, the same as on a rented node), written by `setup` in `bin/python`. A PYTHONPATH the
service's command sets wins (it keeps the module with `:$PYTHONPATH`, `;%PYTHONPATH%` on
Windows); a job run as the node's account has the node's own PYTHONPATH first. A service ships
no copy of it.

**A server kept warm (2.1.2).** `enable … --serve "python server.py"` (or `node.deploy` with
`serve`), beside or instead of `--command`: the node starts the server for a job and keeps it
for the next ones; it loads what it needs once. The job's line comes on its stdin and its end
goes on its stdout (`serve(handle)` in Python, `amsyos-job next` … `done` in any language).
One process serves one service version, one account and one payer (the kernel's
`serve_key`); it is held to a cold job's limits and ends after 10 idle minutes, 100 jobs or an
hour, or when its service changes. A failing job ends the job, not the server; a server that
ends before it takes its job falls back to `--command`; one that ends while it does the job
fails that job (src/serve.rs).

`run` and `try` refuse SYSTEM and root, and an elevated Windows process unless
`--allow-elevated` (a Windows with UAC off has no other kind). Each job runs as `amsyos-job`
in a folder of its own, held by a Job Object (Windows) or rlimits and cgroups (Linux); the
node keeps its key, `config.json`, `services.json` and `node.log` in
`%LOCALAPPDATA%\AMSYOS\Node` or `~/.local/share/amsyos-node`.

**The default `as_node` (2.0.2).** On a machine kept for the organisation's own agents, an
administrator there may make the node's own account the default: `amsyos-node access full
--as-node-default` (or `access as-node-default on` at `full`). Then a service switched on with
`enable` runs as the node's account unless `--as-job` was given, and an admin's `node.deploy`
is `as_node` unless it says `as_node: false`. A job of an order (another organisation) and a
step of a recipe or a mission still run as `amsyos-job`; so does a manager's deploy. `access
as-node-default off`, or any lower level (here or `node.access` from afar), turns it off. Each
job run as the node's account is in `node.log` and in `node.list`'s actions (sdk/NODE.md §5).

**Folders read in place (2.1, spec v3 §8.3).** `amsyos-node allow docs D:\Work\docs` lets the
organisation's jobs read that folder: a service of `compute: node` with an input of `type:
folder` is called with `folder:docs/<path>`, and the node copies what that names into the
job's own folder (read-only for `amsyos-job`; links are never followed) before the job starts.
Only this machine adds or removes one; a session needs `docs` in its grant, a call without one
a manager. Why a copy and not a bind or a link: sdk/NODE.md §6.

A service is defined in the organisation with `compute: node` (`service.define`, no image)
and switched on at the machine with the same id. `examples/text_stats/` is the trial
service in PowerShell (`svc.ps1`) and sh (`svc.sh`), and its spec is in
`scripts/node_check.py`.

## The quick check

```
uv run python scripts/node_check.py local       # a control plane of its own on 127.0.0.1:8111
```

It writes a join code and the service id to `var/node-check/code` and waits for a node; the
Linux binary in a throwaway container (`--network host`) does `setup --user node`, `join
<code> --gateway http://127.0.0.1:8111 --server`, `enable <id> --dir <folder> --command 'sh
svc.sh'` and `run` as the user `node`. The check defines the service (its check runs on the
node), runs it once and prints the report and the time.

`local --levels` writes a third line to the code file, `<org>.node.text_job`. The node's side
then also does `enable <that id> … --as-job` and, as root, `SUDO_USER=node amsyos-node access
full --as-node-default` before `run`. The check prints `RAN AS` for each case: the enabled
service and an admin's deploy run as `node`; `--as-job`, an order's job, `as_node: false` and a
manager's deploy run as `amsyos-job`. When it writes `var/node-check/lowered` (after
`node.access` lowered the level), the node's side runs `SUDO_USER=node amsyos-node access full`
(without the default) and touches `var/node-check/raised`, and the enabled service then runs
as `amsyos-job`.
