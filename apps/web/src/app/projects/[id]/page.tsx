import { ProjectPage } from "@/components/project-page";

export default async function SavedProjectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  // Keyed: another project always starts from a clean watcher state.
  return <ProjectPage key={id} projectId={id} />;
}
