"use client";

import { Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";

type Theme = "light" | "dark";
const STORAGE_KEY = "clipforge-theme";

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
}

export function ThemeToggle({ className }: { className?: string }) {
  function toggle() {
    const next: Theme = document.documentElement.classList.contains("dark") ? "light" : "dark";
    applyTheme(next);
    window.localStorage.setItem(STORAGE_KEY, next);
  }

  return (
    <button
      type="button"
      onClick={toggle}
      className={cn("theme-toggle", className)}
      aria-label="Toggle light and dark mode"
      title="Toggle light and dark mode"
    >
      <Moon className="theme-light-icon size-4" />
      <Sun className="theme-dark-icon size-4" />
      <span className="theme-light-label hidden sm:inline">Dark</span>
      <span className="theme-dark-label hidden sm:inline">Light</span>
    </button>
  );
}
