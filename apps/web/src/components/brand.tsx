import { Sparkles } from "lucide-react";

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <div className="grid size-9 place-items-center rounded-[13px] bg-[#171714] text-white shadow-sm dark:bg-[#f1f0e9] dark:text-[#171714]">
        <Sparkles className="size-[17px]" strokeWidth={2.4} />
      </div>
      {!compact && <span className="text-[17px] font-bold tracking-[-0.03em]">ClipForge</span>}
    </div>
  );
}
