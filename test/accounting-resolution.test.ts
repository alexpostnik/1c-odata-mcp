import { describe, expect, it, vi } from "vitest";
import { Connection } from "../src/context.js";
import { parseConfig } from "../src/config/env.js";
import { accountsByCode, resolveDefaultAccounts } from "../src/odata/accounting.js";
const config = () =>
  parseConfig({ ODATA_BASE_URL: "https://example.invalid/odata/", ODATA_USERNAME: "u", ODATA_PASSWORD: "p" });
function connection() {
  const c = config();
  return new Connection(c.connections[0]!, c.behavior);
}
describe("account and metadata resolution", () => {
  it("shares one concurrent metadata load across Connection and client; isolates bases", async () => {
    const a = connection();
    const b = connection();
    const xml =
      '<edmx:Edmx Version="1.0"><edmx:DataServices><Schema Namespace="StandardODATA"><EntityContainer Name="Container" /></Schema></edmx:DataServices></edmx:Edmx>';
    const first = vi.spyOn(a.client, "getText").mockResolvedValue(xml);
    const second = vi.spyOn(b.client, "getText").mockResolvedValue(xml);
    const [x, y] = await Promise.all([a.getMetadata(), a.client.getMetadata()]);
    expect(x).toBe(y);
    expect(first).toHaveBeenCalledOnce();
    await b.getMetadata();
    expect(second).toHaveBeenCalledOnce();
  });
  it("retries metadata after a failed load", async () => {
    const c = connection();
    const read = vi
      .spyOn(c.client, "getText")
      .mockRejectedValueOnce(new Error("temporary"))
      .mockResolvedValue('<Schema Namespace="StandardODATA" />');
    await expect(c.getMetadata()).rejects.toThrow("temporary");
    await c.client.getMetadata();
    expect(read).toHaveBeenCalledTimes(2);
  });
  it("deduplicates account codes and bounds requests to 40 candidates", async () => {
    const c = connection();
    vi.spyOn(c, "available").mockResolvedValue(new Set(["ChartOfAccounts_Хозрасчетный"]));
    const read = vi.spyOn(c.client, "getCollection").mockResolvedValue({ value: [] });
    await accountsByCode(c, [...Array.from({ length: 41 }, (_, i) => `${i}`), "0"]);
    expect(read).toHaveBeenCalledTimes(2);
    expect(decodeURIComponent(read.mock.calls[1]![0])).toContain("Code eq '40'");
  });
  it("preserves candidate priority and shares one lookup for groups", async () => {
    const c = connection();
    vi.spyOn(c, "available").mockResolvedValue(new Set(["ChartOfAccounts_Хозрасчетный"]));
    const read = vi.spyOn(c.client, "getCollection").mockResolvedValue({
      value: [
        { Code: "62", Ref_Key: "fallback" },
        { Code: "62.01", Ref_Key: "preferred" },
        { Code: "62.02", Ref_Key: "advance" },
      ],
    });
    const resolved = await resolveDefaultAccounts(c, ["receivable", "receivableAdvance", "receivable"]);
    expect(resolved.get("receivable")).toBe("preferred");
    expect(resolved.get("receivableAdvance")).toBe("advance");
    expect(read).toHaveBeenCalledOnce();
    read.mockClear();
    expect(await resolveDefaultAccounts(c, [])).toEqual(new Map());
    expect(read).not.toHaveBeenCalled();
  });
  it("rejects an unavailable configured default in strict mode", async () => {
    const c = connection();
    c.cfg.accounting = { strictDefaults: true, defaults: { receivable: ["62.1"] } };
    vi.spyOn(c, "available").mockResolvedValue(new Set(["ChartOfAccounts_Хозрасчетный"]));
    vi.spyOn(c.client, "getCollection").mockResolvedValue({ value: [] });
    await expect(resolveDefaultAccounts(c, ["receivable"])).rejects.toThrow("receivable");
  });
});
