import type { Metadata } from "next";

import { ServiceWorkerBoot } from "@/components/checkin/service-worker-boot";

/**
 * A separate shell from `(console)/layout.tsx` on purpose.
 *
 * The leadership dashboard's desktop sidebar (`components/nav.tsx`) makes no
 * sense on a phone someone is holding in one hand at the school gate. This
 * layout is deliberately minimal: no nav, no wide max-width column, large
 * touch targets, safe-area padding for devices with a notch or home
 * indicator once installed as a standalone app.
 */
export const metadata: Metadata = {
  title: "Check in — eduCore",
};

export default function CheckinLayout({ children }: LayoutProps<"/checkin">) {
  return (
    <div className="mx-auto min-h-screen w-full max-w-md px-4 pb-10 pt-[max(1.5rem,env(safe-area-inset-top))]">
      <ServiceWorkerBoot />
      {children}
    </div>
  );
}
