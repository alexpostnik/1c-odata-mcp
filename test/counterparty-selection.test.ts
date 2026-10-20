import { describe, expect, it, vi } from "vitest";
import { createServer } from "../src/mcp/server.js";
const G = "22222222-2222-4222-8222-222222222222";
const P = "33333333-3333-4333-8333-333333333333";
function fixture() {
  const row = {
    Ref_Key: G,
    Description: "Synthetic group",
    IsFolder: true,
    Parent_Key: P,
    DeletionMark: false,
    DataVersion: "v1",
  };
  const getCollection = vi.fn(async () => ({ value: [row] }));
  const conn = {
    available: async () => new Set(["Catalog_Контрагенты"]),
    behavior: { pageSize: 100, maxRows: 100 },
    client: { getCollection, getEntity: async () => row },
  };
  const tools = (
    createServer({ db: () => conn } as never) as unknown as {
      _registeredTools: Record<
        string,
        { handler: (args: object, extra: object) => Promise<{ structuredContent?: Record<string, unknown> }> }
      >;
    }
  )._registeredTools;
  return { tools, getCollection };
}
describe("counterparty/group selection", () => {
  it("finds groups explicitly in the same catalog and exposes parent/version", async () => {
    const f = fixture();
    const result = await f.tools["read.counterparty.find_counterparty"]!.handler(
      { query: "Synthetic", subjectKind: "group", limit: 20 },
      {},
    );
    expect(result.structuredContent!.rows).toMatchObject([
      { ref: G, isFolder: true, parentRef: P, dataVersion: "v1", deletionMark: false },
    ]);
    expect(decodeURIComponent(f.getCollection.mock.calls[0]![0] ?? "")).toContain("IsFolder eq true");
  });
  it("card exposes membership and version for safe set_group", async () => {
    const f = fixture();
    const result = await f.tools["read.counterparty.get_counterparty"]!.handler({ ref: G }, {});
    expect(result.structuredContent).toMatchObject({ parentRef: P, dataVersion: "v1", isFolder: true });
  });
});
