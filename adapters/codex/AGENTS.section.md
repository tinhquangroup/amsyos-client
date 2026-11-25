<!-- amsyos -->
{{BRIEF}}

Answers of `amsyos` may end with `around:` lines (`around` in `--json`), what others in this
workspace did since you last saw; read them before you act on shared things (a service, a lease's
`what`, a node).

Run anything that takes more than a minute with `amsyos bg start -- <command>` (it returns at
once; nothing reminds you here, so look with `amsyos bg ls` before you end your turn).

A check due later (a backup tonight, an error rate after a deploy): `amsyos later add "<what>" --at
03:10` (or `--in 2h`); you are told in `around:` when it is due, then `amsyos later done <id>`. Never
read `.env` or print a key: a person puts it in with `amsyos secret set NAME`, and you run with it:
`amsyos secret run --env NAME -- <command>` (its output masked).
<!-- /amsyos -->
