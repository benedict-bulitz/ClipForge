export function splitQuestions(input: string): string[] {
  const questions: string[] = [];
  let paragraph: string[] = [];

  function append(text: string, withQuestionMark = false) {
    const question = `${text.trim()}${withQuestionMark ? "?" : ""}`.replace(/\s+/g, " ");
    if (/[\p{L}\p{N}]/u.test(question)) questions.push(question);
  }

  function flushParagraph() {
    if (!paragraph.length) return;
    let fragment = "";
    for (const character of paragraph.join(" ")) {
      if (character === "?") {
        append(fragment, true);
        fragment = "";
      } else {
        fragment += character;
      }
    }
    append(fragment);
    paragraph = [];
  }

  for (const rawLine of input.replace(/\r\n?/g, "\n").split("\n")) {
    const line = rawLine.trim();
    if (!line) {
      flushParagraph();
      continue;
    }
    const entry = line.match(/^(?:\d+[.)]|[-*•])\s+(.*)$/);
    if (entry) {
      flushParagraph();
      paragraph.push(entry[1]);
    } else {
      paragraph.push(line);
    }
  }
  flushParagraph();
  return questions;
}

export async function submitQuestionsInOrder<T extends { id: string; project_id: string }>(
  questions: readonly string[],
  submit: (question: string) => Promise<T>,
): Promise<{ created: T[]; failed: { index: number; question: string; reason: unknown } | null }> {
  const created: T[] = [];
  for (const [index, question] of questions.entries()) {
    try {
      const job = await submit(question);
      if (!job.id || !job.project_id) throw new Error("The project could not be created.");
      created.push(job);
    } catch (reason) {
      return { created, failed: { index, question, reason } };
    }
  }
  return { created, failed: null };
}
