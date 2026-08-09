import type { MetadataRoute } from "next";

/**
 * The console's one installable identity is staff check-in.
 *
 * `start_url` and `scope` point at `/checkin`, not the leadership dashboard
 * at `/` -- someone adding this to their home screen is a teacher who wants
 * one tap to sign in for the day, not the today/review/coverage screens
 * built for heads and DOSes. Those stay reachable by browser as normal; they
 * are simply outside what this manifest offers to install.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "eduCore Check-in",
    short_name: "Check-in",
    description: "Staff attendance check-in and check-out for eduCore.",
    start_url: "/checkin",
    scope: "/checkin/",
    display: "standalone",
    orientation: "portrait",
    background_color: "#f5f7f6",
    theme_color: "#2b5f8a",
    icons: [
      {
        src: "/favicon.ico",
        sizes: "any",
        type: "image/x-icon",
      },
    ],
  };
}
