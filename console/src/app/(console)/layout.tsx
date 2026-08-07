import { Nav } from "@/components/nav";

export default function ConsoleLayout({ children }: LayoutProps<"/">) {
  return (
    <div className="flex min-h-screen">
      <Nav />
      <div className="min-w-0 flex-1">
        <main className="mx-auto max-w-5xl px-6 py-10 sm:px-8">{children}</main>
      </div>
    </div>
  );
}
