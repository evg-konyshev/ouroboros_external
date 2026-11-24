import type { DocumentSummary } from "@/lib/api";
import { docNameOf } from "@/lib/doc-names";

export function TurnDocumentNames({
  ids,
  docs,
}: {
  ids?: string[];
  docs?: Pick<DocumentSummary, "doc_id" | "filename">[];
}) {
  if (!ids?.length) return null;
  const names = ids.map((id) => docs?.find((doc) => doc.doc_id === id)?.filename || docNameOf(id) || id);
  return (
    <p className="mt-2 break-words text-xs text-[var(--color-neutral-600)]">
      Документы хода: {names.join(", ")}
    </p>
  );
}
