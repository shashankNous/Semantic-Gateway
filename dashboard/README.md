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
| $ saved | `GET /v1/stats` |
| Hit rate chart | `GET /v1/stats/trend` |
| Top cached table | `GET /v1/stats/top-cached` |

None of them take a tenant parameter. The gateway derives the tenant from the dashboard
token this deployment holds, so **one deployment shows exactly one tenant** — there is
no tenant picker in the UI any more, and no way to read another tenant's numbers by
editing a URL.

Cost is deliberately computed client-side. The gateway records how many tokens the cache
avoided sending upstream, but what those tokens would have *cost* depends on which model
you would have called — so the token counts are the durable fact and pricing is a view on
top of them. Prices live in [`lib/pricing.ts`](lib/pricing.ts); edit that file to add a
model or update a rate.

### Why requests go through `/gateway/*`

The browser fetches a same-origin path (`/gateway/v1/stats`), which
[`app/gateway/[...path]/route.ts`](app/gateway/%5B...path%5D/route.ts) forwards to
`$NEXT_PUBLIC_GATEWAY_URL/v1/stats` on the server.

This used to be a plain `rewrites()` entry in `next.config.mjs`. It is a route handler
now because the stats endpoints require a bearer token, and a rewrite cannot attach one
— the browser would have had to hold the credential, which would put it in the page
bundle and in every visitor's devtools. The handler reads `GATEWAY_DASHBOARD_TOKEN`
(no `NEXT_PUBLIC_` prefix, so it stays server-side) at request time and attaches it
itself. The browser never sees a credential and never makes a cross-origin request.

If you would rather have the browser call the gateway directly, set `DASHBOARD_ORIGIN`
on the gateway to this app's origin and point `API_BASE` in
[`lib/gateway.ts`](lib/gateway.ts) at `GATEWAY_URL` — but note that this necessarily
exposes the dashboard token to the browser.

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

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_GATEWAY_URL` | `http://localhost:8000` | Base URL of the FastAPI gateway |
| `GATEWAY_DASHBOARD_TOKEN` | — | Read-only `dash_…` token. **Required**; also picks which tenant this deployment shows. |

`NEXT_PUBLIC_GATEWAY_URL` is read at **build time** (that is what `NEXT_PUBLIC_` means),
so changing it requires a rebuild/redeploy, not just a restart.

`GATEWAY_DASHBOARD_TOKEN` is read at **request time** and must **not** be given a
`NEXT_PUBLIC_` prefix — that would inline it into the client bundle and publish it.
Issue one with `python ../scripts/migrate_hash_keys.py --mode=in-place`, which prints
each new token
once. It is scoped read-only to `/v1/stats*`: it cannot call the proxy route, so a leak
exposes usage statistics but cannot spend money on LLM calls. Rotate it by issuing a new
token and updating this variable.

Do not point `NEXT_PUBLIC_GATEWAY_URL` at a docker-internal hostname like
`http://app:8000`. The rewrite is resolved by the Next server, so it must be a host that
server can reach; use `http://localhost:8000` locally and a public HTTPS URL in production.

## Running locally

The gateway must be running first (default `http://localhost:8000`), with Postgres
reachable and migrations `001`–`006` applied.

If you have no traffic yet, seed a demo tenant so there is something to look at — this
also creates an empty tenant for checking the loading and empty states:

```bash
python ../scripts/seed_demo.py
```

That prints each demo tenant's credentials. Use `dash_demo-tenant-token` for the
tenant with data, or `dash_demo-empty-token` to check the empty states. Then:

```bash
cd dashboard
npm install
```

```bash
cp .env.example .env.local
```

`.env.example` already points at the demo tenant's token, so it works as-is against a
seeded local database. Then start the dev server:

```bash
npm run dev
```

Open <http://localhost:3000>. **Refresh** re-polls. To look at a different tenant,
change `GATEWAY_DASHBOARD_TOKEN` in `.env.local` and restart.

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

And the dashboard token, which decides which tenant this deployment shows:

```bash
vercel env add GATEWAY_DASHBOARD_TOKEN production
```

Then deploy:

```bash
vercel deploy
```

```bash
vercel deploy --prod
```

Alternatively, in the Vercel dashboard: import the repo, set **Root Directory** to
`dashboard`, and add both variables under **Settings → Environment Variables**.
Framework preset and build commands are detected automatically.

Because `NEXT_PUBLIC_*` values are inlined at build time, changing the gateway URL in
Vercel requires a redeploy for it to take effect.

### A note on exposure

`/v1/stats*` now requires a scoped bearer token and derives the tenant from it, so the
old "change the number in the URL to read another tenant" hole is closed.

What remains is that **anyone who can load this deployment sees this tenant's stats** —
the token lives on the Vercel server, not behind a user login, so the page itself is the
authorization boundary. Before putting it in front of a real tenant, put it behind
Vercel's password protection (or an SSO integration), and give each tenant its own
deployment with its own token rather than sharing one.
