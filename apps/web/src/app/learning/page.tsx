import { redirect } from "next/navigation";

/** Learning History is part of the Video Library now: archived videos, one destination. */
export default function LearningPage() {
  redirect("/videos?project=archived");
}
