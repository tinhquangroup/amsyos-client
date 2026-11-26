---
name: amsyos
description: Use AMSYOS when the work needs a GPU or more compute than this machine has, must outlive this session, when code is shared with other agents or organisations in a git repository on AMSYOS, can use a service of the organisation's own or one offered to it, when this repository is deployed as an AMSYOS service (amsyos.yaml), or to recall what earlier work on this project built, broke and fixed. Works through the amsyos command.
---

{{BRIEF}}

## Reading its answers

- The project's brief, where `amsyos init` ran: `.amsyos/plugins/amsyos/skills/amsyos/brief.md`
  in the repository, written by `amsyos sync` (its SessionStart hook too). Read it first.
- `amsyos run --wait`, `amsyos wait <job>`: wait for the end. Exit 0: done; 1: failed or refused; 3: it waits for you
  or a person (`amsyos wait` holds until they decide), or `--timeout` passed. A check's test prints what it read.
- `eta_s` is the wait until the result (`start_s` of it: a new machine starting), `est_cost`
  the credit ("default estimate": no run yet); `amsyos result`: the real cost and time, by step.
- `amsyos deploy` prints the files it packs, then `service@version` with the check of the
  card's example: exit 0 when defined (a check still running says its job), 1 on a real
  failure with the hint (`amsyos logs <job>` for a failed build). A node given back says `closed`.
- `as inputs` lists the `asset://` references of outputs another service takes with no upload.
