# FeohLedger — Frontend

SvelteKit 3 + Svelte 5 + TypeScript

See [`CLAUDE.md`](CLAUDE.md) for full documentation (structure, stores, routes, API mappings).

## Quick Start

```bash
pnpm i
pnpm dev
```

Dev server runs on http://localhost:7777

No env setup needed — `.env.development` is committed with the safe local
default (`PUBLIC_API_URL=http://localhost:8000`) and Vite loads it
automatically in dev mode. For a personal override, create a gitignored
`.env.local` (it wins over `.env.development`).
