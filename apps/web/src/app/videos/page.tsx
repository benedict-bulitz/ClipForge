import type { Metadata } from "next";
import { VideoLibrary } from "@/components/video-library";
import { parseLibraryFilters } from "@/lib/videos";

export const metadata: Metadata = {
  title: "Videos — ClipForge",
  description: "Every video uploaded to YouTube through ClipForge, with its status and performance history.",
};

export default async function VideosPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  return <VideoLibrary initialFilters={parseLibraryFilters(await searchParams)} />;
}
