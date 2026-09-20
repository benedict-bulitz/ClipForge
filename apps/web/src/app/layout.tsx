import type { Metadata } from "next";
import "./globals.css";

const themeScript = `(() => {
  try {
    const saved = localStorage.getItem("clipforge-theme");
    const dark = saved === "dark" || (saved !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("dark", dark);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
  } catch (_) {}
})();`;

export const metadata: Metadata = {
  title: "ClipForge — Idea to shortform video",
  description: "Describe the idea. ClipForge directs the rest.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeScript }} /></head>
      <body>{children}</body>
    </html>
  );
}
