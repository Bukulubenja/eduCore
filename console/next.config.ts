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
};

export default nextConfig;
