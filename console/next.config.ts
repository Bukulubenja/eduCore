import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin the workspace root. Next otherwise walks up looking for a lockfile or
  // .git to infer it, and this tree has a .git directory left unreadable by an
  // interrupted delete, which makes that walk fail with EPERM.
  turbopack: {
    root: path.resolve(__dirname),
  },
  outputFileTracingRoot: path.resolve(__dirname),

  async headers() {
    return [
      {
        // Never let a CDN or the browser cache the service worker itself --
        // a stale worker stuck serving an old cached shell is worse than no
        // worker at all, and this file is the one thing that must always be
        // re-fetched to pick up a new version.
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Content-Type", value: "application/javascript; charset=utf-8" },
        ],
      },
    ];
  },
};

export default nextConfig;
