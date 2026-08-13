/**
 * The dashboard fetches from a same-origin `/gateway/*` path, which Next rewrites
 * to the real FastAPI gateway on the server side. Going straight from the browser
 * to the gateway would need CORS headers on the Python app, which this dashboard
 * deliberately does not require — the gateway stays untouched.
 */
const GATEWAY_URL = (
  process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000"
).replace(/\/+$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/gateway/:path*",
        destination: `${GATEWAY_URL}/:path*`,
      },
    ];
  },
};

export default nextConfig;
