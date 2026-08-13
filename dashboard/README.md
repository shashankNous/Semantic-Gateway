# Semantic Gateway — Dashboard

A Next.js 14 (App Router, TypeScript, Tailwind) dashboard for the caching LLM gateway.

It shows, for one tenant:

- **$ saved by caching** — computed in the browser from the `prompt_tokens_saved` and
  `completion_tokens_saved` the gateway reports, priced against a model you pick from a
  dropdown. Switching models reprices instantly; no refetch.
- **Hit rate over the last 7 days** — a Recharts line chart fed by `/v1/stats/trend`.
- **Top 20 cached queries** — hit count and the similarity range each entry has served.

## Data flow

The dashboard **never touches Postgres**. Everything comes from the FastAPI gateway:

| Panel | Endpoint |
| --- | --- |
| $ saved | `GET /v1/stats?tenant_id=…` |
| Hit rate chart | `GET /v1/stats/trend?tenant_id=…` |
| Top cached table | `GET /v1/stats/top-cached?tenant_id=…` |

Cost is deliberately computed client-side. The gateway records how many tokens the cache
avoided sending upstream, but what those tokens would have *cost* depends on which model
you would have called — so the token counts are the durable fact and pricing is a view on
top of them. Prices live in [`lib/pricing.ts`](lib/pricing.ts); edit that file to add a
model or update a rate.

### Why requests go through `/gateway/*`

The browser fetches a same-origin path (`/gateway/v1/stats`), which
[`next.config.mjs`](next.config.mjs) rewrites server-side to
`$NEXT_PUBLIC_GATEWAY_URL/v1/stats`. Calling the gateway straight from the browser would
require CORS headers on the Python app, and this dashboard is built not to need any change
to the gateway. If you would rather have the browser hit the gateway directly, add
`CORSMiddleware` to `app/main.py` and point `API_BASE` in [`lib/gateway.ts`](lib/gateway.ts)
at `GATEWAY_URL`.

## Theme

Two brand colours drive the whole page:

| Token | Hex | Role |
| --- | --- | --- |
| `brand` | `#035352` | Authentic teal — card surfaces |
| `brand-deep` | `#012322` | Page ground, nested tiles, inputs |
| `sidecar` | `#F3E8BC` | Sidecar yellow — primary text, the `$` figure, the chart line |

They are declared once in [`tailwind.config.ts`](tailwind.config.ts), and mirrored in
[`lib/theme.ts`](lib/theme.ts) for Recharts, which needs raw colour strings rather than
Tailwind classes. Change a hex in both places and the whole dashboard follows.

Everything else is sidecar at reduced alpha (`text-sidecar/75`, `border-sidecar/15`) rather
than new hues, which keeps the palette to two colours. One constraint worth preserving if
you retune it: sidecar at 50% on teal is only **2.98:1**, below the WCAG AA 4.5:1 floor for
body text, so muted text bottoms out at `/75` (4.79:1). That includes the chart's axis tick
labels — `CHART.axis` colours real text, not just a line.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_GATEWAY_URL` | `http://localhost:8000` | Base URL of the FastAPI gateway. **This is the one to set.** |
| `NEXT_PUBLIC_TENANT_ID` | `1` | Tenant loaded on first paint. Editable in the UI. |

Both are read at **build time** (that is what `NEXT_PUBLIC_` means), so changing either
one requires a rebuild/redeploy — not just a restart of an existing build.

Do not point `NEXT_PUBLIC_GATEWAY_URL` at a docker-internal hostname like
`http://app:8000`. The rewrite is resolved by the Next server, so it must be a host that
server can reach; use `http://localhost:8000` locally and a public HTTPS URL in production.

## Running locally

The gateway must be running first (default `http://localhost:8000`), with Postgres
reachable and migrations `001`–`004` applied.

If you have no traffic yet, seed a demo tenant so there is something to look at — this
also creates an empty tenant for checking the loading and empty states:

```bash
python ../scripts/seed_demo.py
```

That prints the tenant ids to use (`1` has data, `2` is empty). Then:

```bash
cd dashboard
npm install
```

```bash
cp .env.example .env.local
```

Then start the dev server:

```bash
npm run dev
```

Open <http://localhost:3000>. Use the **Tenant ID** field in the header to switch tenants
and **Refresh** to re-poll.

To verify against a non-local gateway without editing files:

```bash
NEXT_PUBLIC_GATEWAY_URL=https://your-gateway.up.railway.app npm run dev
```

Production build:

```bash
npm run build
```

## Deploying with `vercel deploy`

From inside `dashboard/` (it is a subdirectory of the repo, so deploy from here rather
than the repo root — otherwise Vercel will try to build the Python app):

```bash
npm i -g vercel
```

```bash
vercel link
```

Set the gateway URL for each environment you deploy to. It must be a **publicly reachable
HTTPS URL** — `localhost` will not resolve from Vercel's servers:

```bash
vercel env add NEXT_PUBLIC_GATEWAY_URL production
```

Then deploy:

```bash
vercel deploy
```

```bash
vercel deploy --prod
```

Alternatively, in the Vercel dashboard: import the repo, set **Root Directory** to
`dashboard`, and add `NEXT_PUBLIC_GATEWAY_URL` under **Settings → Environment Variables**.
Framework preset and build commands are detected automatically.

Because `NEXT_PUBLIC_*` values are inlined at build time, changing the gateway URL in
Vercel requires a redeploy for it to take effect.

### A note on exposure

`/v1/stats*` currently takes a `tenant_id` query parameter and no API key, so anyone who
can reach a deployed dashboard can read any tenant's stats by changing the number. Keep
the deployment behind Vercel's password protection, or add auth to the stats endpoints,
before putting this in front of real tenants.
