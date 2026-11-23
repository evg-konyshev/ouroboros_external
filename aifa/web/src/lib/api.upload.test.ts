import { afterEach, describe, expect, it, vi } from "vitest";

import { uploadDocument } from "./api";

afterEach(() => vi.unstubAllGlobals());

function captureFormData(): { body: FormData | undefined } {
  const captured = { body: undefined as FormData | undefined };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init?: RequestInit) => {
      captured.body = init?.body as FormData | undefined;
      return new Response(JSON.stringify({ doc_id: "doc-1" }), { status: 200 });
    }),
  );
  return captured;
}

describe("uploadDocument workspace_id", () => {
  it("includes workspace_id in the body when provided", async () => {
    const captured = captureFormData();
    const file = new File(["x"], "x.pdf", { type: "application/pdf" });

    await uploadDocument(file, { workspace_id: "folder-42" });

    expect(captured.body).toBeDefined();
    expect(captured.body!.get("workspace_id")).toBe("folder-42");
  });

  it("omits workspace_id from the body when not provided", async () => {
    const captured = captureFormData();
    const file = new File(["x"], "x.pdf", { type: "application/pdf" });

    await uploadDocument(file);

    expect(captured.body).toBeDefined();
    expect(captured.body!.has("workspace_id")).toBe(false);
  });
});
