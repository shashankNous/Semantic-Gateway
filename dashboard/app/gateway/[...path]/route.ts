import { NextRequest, NextResponse } from "next/server";

/**
 * Server-side proxy to the FastAPI gateway.
 *
 * This replaces the plain `rewrites()` entry that used to live in next.config.mjs.
 * The stats endpoints now require a `dash_...` bearer token, and the point of this
 * handler is that the token is attached **here**, on the Next server, from a
 * non-`NEXT_PUBLIC_` env var. A rewrite would have forced the browser to hold the
 * token, which would put it in the page bundle and in every user's devtools.
 *
 * The browser therefore still fetches a same-origin `/gateway/*` path and still
 * never sees a credential.
 */

const GATEWAY_URL = (
  process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000"
).replace(/\/+$/, "");

/** Read at request time, not build time — no NEXT_PUBLIC_ prefix, so it stays server-side. */
const DASHBOARD_TOKEN = process.env.GATEWAY_DASHBOARD_TOKEN;

/** The tenant comes from the token, so responses are per-deployment, never cacheable. */
export const dynamic = "force-dynamic";

export async function GET(
  request: NextRequest,
  { params }: { params: { path: string[] } },
) {
  if (!DASHBOARD_TOKEN) {
    return NextResponse.json(
      {
        detail:
          "GATEWAY_DASHBOARD_TOKEN is not set on the dashboard server. " +
          "Issue one with scripts/migrate_hash_keys.py --mode=in-place, " +
          "then set it in the environment.",
      },
      { status: 500 },
    );
  }

  const target = `${GATEWAY_URL}/${params.path.join("/")}`;

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      headers: {
        Authorization: `Bearer ${DASHBOARD_TOKEN}`,
        Accept: "application/json",
      },
      cache: "no-store",
      signal: request.signal,
    });
  } catch (error) {
    return NextResponse.json(
      { detail: `Could not reach the gateway at ${GATEWAY_URL}` },
      { status: 502 },
    );
  }

  const body = await upstream.text();

  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") ?? "application/json",
      "Cache-Control": "no-store",
    },
  });
}
