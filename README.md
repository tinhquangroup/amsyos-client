# AMSYOS client

The open-source part of [AMSYOS](https://103.77.173.205/cli): a cloud OS for work shared by
people, AI agents and services. What runs on your machine is here, under Apache-2.0:

- `cli/`: the `amsyos` command, which AI coding agents (Claude Code, Codex, Cursor,
  Antigravity) and people use to find, run and deploy services, keep project memory, and
  work beside other agents. It talks to AMSYOS only through its six MCP tools.
- `sdk/`: the service contract (`CONTRACT.md`: the card, the job contract, the check, how a
  card maps to MCP and A2A) and the job's bindings (Python, Node, `amsyos-job`).
- `node/`: AMSYOS Node, the one program an organisation's own computer runs to serve its
  services (`sdk/NODE.md` is its protocol).
- `adapters/`: the brief, the skill, the hooks and the MCP entries the CLI installs for each
  host, and `desk/`, the rules and log `amsyos init --type studio|work` writes for a desk.

The AMSYOS kernel (scheduling, isolation, billing, the knowledge graph) is not in this
repository.

## Install the CLI

The same command on Windows, macOS and Linux (it needs [uv](https://docs.astral.sh/uv/)):

    uvx --from https://103.77.173.205/cli/amsyos_cli-0.4.11-py3-none-any.whl amsyos install

Then `amsyos login`, and later `amsyos self-update`. `/cli/latest.json` on the Gateway names
the newest wheel with its sha256.

## Build from this repository

    uv build --wheel cli          # the CLI's wheel (it carries adapters/ and sdk/)
    node/build.sh                 # AMSYOS Node in Docker

## License

Apache License 2.0: see LICENSE and NOTICE. This repository is exported from AMSYOS's own
repository; changes are taken there and published here with the next export.
