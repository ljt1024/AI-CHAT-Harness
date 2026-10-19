# AgentForge implementation

The two Chinese planning documents define the roadmap. Task book v3 is authoritative.
This repository imports AI-CHAT 2.1.1 at `fc78865fcea983503535f46f889886f33b28e872`.
The upstream `end/` directory is preserved as reference, including its dependencies.

## Prerequisites

- Node.js 22.13+, npm, Python 3.12+, uv, Docker Desktop/Engine.
- Configure local `.env` using the variable names in `.env.example`.
- Never commit `.env` or pass model credentials into tool processes.

## Infrastructure

```sh
docker compose up -d mongo
cd harness
uv run pytest tests/unit/test_config.py -q
```

## Base frontend

```sh
npm ci
npm run dev
npm run build
npm run check:i18n
```

The original backend can be started for contract comparison with
`npm ci --prefix end` and `npm run dev --prefix end`.

## Progress

- T0: Base import, runtime package, configuration, MongoDB compose, CI and ADR.
- Remaining tasks must pass their individual acceptance commands before completion.
