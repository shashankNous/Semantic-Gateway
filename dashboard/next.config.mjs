/**
 * The `/gateway/*` proxy used to be a rewrite here. It now lives in
 * app/gateway/[...path]/route.ts, because the stats endpoints require a bearer
 * token and a rewrite cannot attach one server-side — the browser would have had
 * to hold the credential.
 *
 * NEXT_PUBLIC_GATEWAY_URL is still read by that route handler and shown in the UI.
 */

/** @type {import('next').NextConfig} */
const nextConfig = {};

export default nextConfig;
