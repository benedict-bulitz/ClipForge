import type { Metadata } from "next";
import { IntegrationsSettings } from "@/components/integrations-settings";

export const metadata: Metadata = {
  title: "Integrations — ClipForge Settings",
  description: "Connect the services ClipForge uses for planning, research, voice, and media.",
};

export default function IntegrationsPage() {
  return <IntegrationsSettings />;
}
