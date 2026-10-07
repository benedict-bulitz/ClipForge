import {
  ArrowLeft,
  Boxes,
  Clapperboard,
  Database,
  PlugZap,
  Settings2,
  SlidersHorizontal,
} from "lucide-react";
import { Brand } from "./brand";
import { Button } from "./ui/button";
import { BackLink } from "./back-link";
import { ThemeToggle } from "./theme-toggle";
import { PageLink } from "./page-link";

const futureSections = [
  { label: "System", icon: Settings2 },
  { label: "Storage", icon: Database },
  { label: "Rendering", icon: SlidersHorizontal },
];

export function SettingsShell({ children }: { children: React.ReactNode }) {
  return (
    <main className="theme-app relative min-h-screen overflow-hidden bg-[#eeece4]">
      <div className="noise" />
      <header className="sticky top-0 z-40 border-b border-[var(--border)] bg-[var(--surface)] backdrop-blur-xl">
        <div className="mx-auto flex min-h-16 max-w-[1320px] items-center justify-between gap-4 px-4 py-2 sm:px-6 lg:px-8">
          <PageLink href="/" aria-label="ClipForge home" className="rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#ff6838]/40">
            <Brand />
          </PageLink>
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <Button asChild variant="ghost" size="sm">
              <PageLink href="/videos"><Clapperboard className="size-3.5" /> Videos</PageLink>
            </Button>
            <Button asChild variant="ghost" size="sm">
              <BackLink href="/" match="any"><ArrowLeft className="size-3.5" /> Back</BackLink>
            </Button>
          </div>
        </div>
      </header>

      <div className="relative mx-auto grid max-w-[1320px] gap-7 px-4 pb-20 pt-7 sm:px-6 md:grid-cols-[210px_minmax(0,1fr)] lg:gap-12 lg:px-8 lg:pt-10">
        <aside>
          <div className="md:sticky md:top-24">
            <div className="mb-4 flex items-center gap-2 px-2">
              <Boxes className="size-4 text-[#ff6838]" />
              <span className="text-sm font-bold">Settings</span>
            </div>
            <nav aria-label="Settings sections" className="flex gap-2 overflow-x-auto pb-2 md:block md:space-y-1 md:overflow-visible md:pb-0">
              <PageLink
                href="/settings/integrations"
                aria-current="page"
                className="flex shrink-0 items-center gap-2.5 rounded-xl bg-[#171714] px-3.5 py-2.5 text-sm font-semibold text-white shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#ff6838]/40"
              >
                <PlugZap className="size-4" /> Integrations
              </PageLink>
              {futureSections.map(({ label, icon: Icon }) => (
                <span
                  key={label}
                  aria-disabled="true"
                  className="flex shrink-0 cursor-default items-center gap-2.5 rounded-xl px-3.5 py-2.5 text-sm font-semibold text-[#9a9a91] md:w-full"
                >
                  <Icon className="size-4" /> {label}
                  <span className="mono ml-auto hidden text-[8px] uppercase tracking-[.08em] text-[#afafa6] md:inline">Later</span>
                </span>
              ))}
            </nav>
          </div>
        </aside>
        <section className="min-w-0">{children}</section>
      </div>
    </main>
  );
}
