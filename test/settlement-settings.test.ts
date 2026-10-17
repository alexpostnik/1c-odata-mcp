import { describe, expect, it, vi } from "vitest";
import { createServer } from "../src/mcp/server.js";
import type { EntityMeta } from "../src/types/odata.js";

const entity = (name: string, props: string[], keys: string[] = []): EntityMeta => ({
  entitySet: name,
  entityType: name,
  class: name.startsWith("InformationRegister_") ? "informationRegister" : "catalog",
  shortName: name,
  keys,
  properties: props.map((name) => ({
    name,
    type: name.endsWith("_Key") ? "Edm.Guid" : "Edm.String",
    nullable: true,
  })),
  navigations: [],
});
const set = "InformationRegister_SyntheticSettings";
const dimensions = {
  organization: "Org_Key",
  counterparty: "Party_Key",
  contract: "Agreement_Key",
  settlementKind: "Kind_Key",
};
const resources = { settlementAccount: "Ledger_Key", advanceAccount: "Advance_Key" };
const mapping = { entitySet: set, dimensions, resources };
const fields = [...Object.values(dimensions), ...Object.values(resources)];
function fixture(entities = [entity(set, fields, Object.values(dimensions))]) {
  const getMetadata = vi.fn(async () => ({
    odataVersion: "3.0",
    entities: new Map(entities.map((e) => [e.entitySet, e])),
  }));
  const getCollection = vi.fn();
  const create = vi.fn();
  const patch = vi.fn();
  const conn = { cfg: { name: "synthetic" }, getMetadata, client: { getCollection, create, patch } };
  const server = createServer({ db: () => conn } as never) as unknown as {
    _registeredTools: Record<
      string,
      {
        handler: (
          args: Record<string, unknown>,
          extra: object,
        ) => Promise<{ isError?: boolean; structuredContent?: Record<string, unknown> }>;
        outputSchema: { safeParse: (value: unknown) => { success: boolean } };
      }
    >;
  };
  return { ...conn, tool: server._registeredTools["read.schema.discover_settlement_settings"] };
}

describe("settlement account settings metadata discovery", () => {
  it("lists only published information registers with exact keys, fields and unknown roles", async () => {
    const f = fixture([
      entity(set, fields, Object.values(dimensions)),
      entity("Catalog_BankAccounts", ["AccountNumber", "Ref_Key"]),
    ]);
    expect(f.tool).toBeDefined();
    const result = await f.tool!.handler({ limit: 50 }, {});
    expect(result.structuredContent).toMatchObject({
      status: "mapping_required",
      candidates: [{ entitySet: set, keys: Object.values(dimensions), roles: "unknown" }],
    });
    expect(f.tool!.outputSchema.safeParse(result.structuredContent).success).toBe(true);
    expect(f.getMetadata).toHaveBeenCalledOnce();
    expect(f.client.getCollection).not.toHaveBeenCalled();
    expect(f.client.create).not.toHaveBeenCalled();
    expect(f.client.patch).not.toHaveBeenCalled();
  });
  it("validates explicit caller dimension/resource fields without inferring role semantics or write support", async () => {
    const f = fixture();
    const result = await f.tool!.handler({ mapping, limit: 50 }, {});
    expect(result.structuredContent).toMatchObject({
      status: "mapping_fields_verified",
      mapping: { dimensions, resources, roles: "caller_supplied_unverified", genericWriteSupport: "unknown" },
    });
    expect(f.tool!.outputSchema.safeParse(result.structuredContent).success).toBe(true);
  });
  it("reports a missing target as not published, differently named, or unknown rather than absent functionality", async () => {
    const f = fixture([]);
    const result = await f.tool!.handler({ mapping, limit: 50 }, {});
    expect(result.structuredContent).toMatchObject({
      status: "not_published_or_different_name_or_unknown",
      candidates: [],
    });
    expect(f.client.getCollection).not.toHaveBeenCalled();
  });
  it("rejects missing or reused mapped fields and does not substitute bank or contract fields", async () => {
    const f = fixture();
    const result = await f.tool!.handler(
      { mapping: { ...mapping, resources: { settlementAccount: "BankAccountNumber" } }, limit: 50 },
      {},
    );
    expect(result.structuredContent).toMatchObject({
      status: "invalid_mapping",
      mapping: { missingFields: ["BankAccountNumber"] },
    });
    const reused = await f.tool!.handler(
      { mapping: { ...mapping, resources: { settlementAccount: dimensions.contract } }, limit: 50 },
      {},
    );
    expect(reused.structuredContent).toMatchObject({
      status: "invalid_mapping",
      mapping: { duplicateFields: [dimensions.contract] },
    });
    expect(f.client.getCollection).not.toHaveBeenCalled();
  });
  it("bounds metadata output, preserves incomplete status and searches actual field names", async () => {
    const f = fixture([
      entity(set, fields),
      entity("InformationRegister_Second", ["Ledger_Key"]),
      entity("InformationRegister_Other", ["Unrelated"]),
    ]);
    const result = await f.tool!.handler({ search: "Ledger_Key", limit: 1 }, {});
    expect(result.structuredContent).toMatchObject({
      count: 1,
      matchingCount: 2,
      truncated: true,
      status: "mapping_required",
    });
    const missing = await f.tool!.handler({ search: "NoSuchObject", limit: 50 }, {});
    expect(missing.structuredContent?.status).toBe("not_published_or_different_name_or_unknown");
  });
});
