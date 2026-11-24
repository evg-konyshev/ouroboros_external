import { afterEach, expect, it, vi } from "vitest";

import { startChatTurn } from "@/lib/api";

afterEach(() => vi.unstubAllGlobals());

it("keeps the ordinary composer request and sends an empty document list for the folder", async () => {
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => Response.json({ run_id: "r" }));
  vi.stubGlobal("fetch", fetchMock);
  const input = { message: "Сравни отчёты", doc_ids: ["attached"], project_id: "folder" };

  await startChatTurn(input);
  await startChatTurn({ ...input, scope: "all_workspace" });

  const [ordinary, workspace] = fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init.body)));
  expect(ordinary).toEqual({
    session_id: "", message: input.message, doc_ids: ["attached"], project_id: "folder",
    arch: "", role: "", pinned_tool: "", pinned_tool_args: {}, web_search: true, active_doc_id: "",
  });
  expect(workspace).toEqual({ ...ordinary, scope: "all_workspace", doc_ids: [] });
});
