import type { Metadata } from "next";
import { VideoDetailPage } from "@/components/video-detail";

export const metadata: Metadata = {
  title: "Video — ClipForge",
  description: "Status, analytics, retention and production context of one uploaded video.",
};

export default async function VideoPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  // Keyed by the stable upload id: works whether or not the project still exists.
  return <VideoDetailPage key={id} videoId={id} />;
}
