import type { Metadata } from "next";
import { QueueOverviewPage } from "@/components/queue-overview";

export const metadata: Metadata = {
  title: "Queue — ClipForge",
  description: "The current generation queue: every queued, generating and finished video with its preview and publishing state.",
};

export default function QueuePage() {
  return <QueueOverviewPage />;
}
