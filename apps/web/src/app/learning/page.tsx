import type { Metadata } from "next";
import { LearningHistory } from "@/components/learning-history";

export const metadata: Metadata = {
  title: "Learning History — ClipForge",
  description: "Performance records of uploaded videos whose local project was deleted.",
};

export default function LearningPage() {
  return <LearningHistory />;
}
