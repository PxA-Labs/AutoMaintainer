import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: 'export',
  // Emit every route as <route>/index.html. The export is also served by
  // FastAPI StaticFiles(html=True), which resolves directory index files but
  // not '<route>.html', so without this /admin and /auth/callback 404 there.
  trailingSlash: true,
};

export default nextConfig;
