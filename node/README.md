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
amsyos-node profiles                               2.1.9: the nodes (profiles) of this account here
amsyos-node --profile <name> <command> …           2.1.9: the same, on a second node beside the first
amsyos-node profile adopt <name> <folder>          2.1.9: a node folder made by hand becomes a profile
```

**One computer, several organisations (2.1.9).** A node is one organisation's. For a second
organisation on the same computer, join a second node beside the first as a *profile*:

```
amsyos-node --profile acme setup [--jobs D:\AMSYOS\acme\jobs] [--services D:\AMSYOS\acme\services]
amsyos-node --profile acme join <code> --gateway https://<host>
```

Every command takes `--profile <name>` (or `AMSYOS_NODE_PROFILE`): its own node folder
(`%LOCALAPPDATA%\AMSYOS\Node-acme`, `~/.local/share/amsyos-node-acme`) with its own key,
config, services and logs, and by default its own jobs and managed services folders
(`%ProgramData%\AMSYOS\Node\jobs-acme`, `…\services-acme`). Without one, nothing moves.
On Windows each profile runs from a scheduled task of its own, "AMSYOS Node acme", running
`amsyos-node.exe --profile acme run --allow-elevated`; `setup` and `join` print the
PowerShell line that makes it (at the user's logon, highest rights, no time limit).
`amsyos-node profiles` lists them. `setup` writes the job account's renewed password into every
node folder of the user, so running it for one profile never stops another's jobs. The
profiles share the program, the account it runs as and `amsyos-job`: at `full` an
organisation's admins reach the other nodes too (`join` and `access full` warn). Keep profiles
for organisations that trust each other.

A node made by hand before 2.1.9 (another `LOCALAPPDATA` for a second process) becomes a
profile with `amsyos-node profile adopt <name> <its node folder>`, with that node stopped: the
folder moves to `Node-<name>` with its key, and the jobs and services folders its config.json
names stay; then replace its old task with the line `adopt` prints.

**A new version of the program (2.1.9: by itself).** `amsyos-node auto-update on|off` (an
administrator at the machine; on for a node joined since 2.1.9, off for one joined before),
`amsyos-node update [--check]` (now) and `amsyos-node rollback` (back to the program before).
With auto-update on, the node asks its own Gateway at its start and every 6 hours. It takes only
a newer version signed with AMSYOS's release key (built into the program), of the size and
SHA-256 signed, that answers `version`. It puts the release in place while it runs no job (a
`node.exec` counts). Windows: the program in `%ProgramFiles%\AMSYOS\Node` and `amsyos-job.exe`,
the old ones kept as `…prev.exe`. Linux: the installed program when the node's account may
write it, else a verified copy in the node's folder that `run` execs into. Then every node
process on the machine restarts itself once it runs no job. On Windows a one-shot scheduled task
ends and starts the task that runs it. `node.log` says each step (sdk/NODE.md §7). The release:
`node/build.sh`, then `uv run python scripts/node_release.py <version>`, which signs it and
writes `dist/node/`, served at `/node/release/`.

By hand, as before: stop the node (its scheduled task, or Ctrl+C; every profile's), run the new file's
`setup` again in an administrator window (it copies the program into `%ProgramFiles%\AMSYOS\Node`
and sets the folders' ACLs again; since 2.1.9 it keeps the `--jobs` and `--services` folders
config.json names), then start the node again. Join, services, the level and its default are
kept. On Linux, run `sudo amsyos-node setup` again the same way: it copies `amsyos-job` and the
Python job module that every job uses (`run` warns while they are not this version's; a
self-update on Linux cannot replace root's `amsyos-job`).

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

**Undeploy and builds (2.1.5).** `node.undeploy` (a manager, the node at `managed` or `full`)
removes what a deploy put here: the service's warm servers, its entry and its folder. After an
undeploy, a deploy and at its start the node also removes the builds `amsyos deploy` made under
`~/amsyos-build` (or `AMSYOS_BUILD_HOME`) that no deployment uses, never one younger than an
hour nor while a `node.exec` runs, and says what each deployment holds of the disk
(src/builds.rs, sdk/NODE.md §4).

**A job's log (2.1.6).** The node keeps its jobs' logs (`logs/<job>.log` in its own folder, at
most 1 MB each): the last 50 jobs', and since 2.1.7 every one of the last 7 days while they all
stay within 500 MB and 10,000 files, the oldest removed first. `amsyos logs j_… --full` (the service `node.job_log`) asks for
one: the node puts it, at most its last 5 MB, to a link the kernel signed, and the kernel keeps
it as a file of the job's workspace. Only for the organisation's own job, at any level, while the
node is online and still has it (src/net.rs, sdk/NODE.md §5).

`run` and `try` refuse SYSTEM and root, and an elevated Windows process unless
`--allow-elevated` (a Windows with UAC off has no other kind). Each job runs as `amsyos-job`
in a folder of its own, held by a Job Object (Windows) or rlimits and cgroups (Linux); the
node keeps its key, `config.json`, `services.json` and `node.log` in
`%LOCALAPPDATA%\AMSYOS\Node` or `~/.local/share/amsyos-node` (a profile's: `…-<name>`).

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
