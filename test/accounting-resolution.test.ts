import { describe, expect, it, vi } from "vitest";
import { Connection } from "../src/context.js";
import { parseConfig } from "../src/config/env.js";
import {
  accountsByCode,
  accountsByRef,
  resolveAccounts,
  resolveDefaultAccounts,
} from "../src/odata/accounting.js";
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
      .mockResolvedValue(
        '<Edmx><DataServices><Schema Namespace="StandardODATA"><EntityContainer Name="Container" /></Schema></DataServices></Edmx>',
      );
    await expect(c.getMetadata()).rejects.toThrow("temporary");
    await c.client.getMetadata();
    expect(read).toHaveBeenCalledTimes(2);
  });
  it("план счетов грузится один раз на подключение и обслуживает все поиски", async () => {
    const c = connection();
    vi.spyOn(c, "available").mockResolvedValue(new Set(["ChartOfAccounts_Хозрасчетный"]));
    const read = vi.spyOn(c.client, "getCollection").mockResolvedValue({
      value: [
        { Code: "62.01", Ref_Key: "A1", Description: "Покупатели" },
        { Code: "60.01", Ref_Key: "b2", Description: "Поставщики" },
      ],
    });
    expect((await accountsByCode(c, ["62.01", "62.01", "99"])).get("62.01")).toBe("A1");
    expect((await resolveAccounts(c, ["60"])).map((a) => a.key)).toEqual(["b2"]);
    expect([...(await accountsByRef(c, ["a1"])).values()][0]!.code).toBe("62.01");
    expect(read).toHaveBeenCalledOnce();
    expect(decodeURIComponent(read.mock.calls[0]![0])).not.toContain("$filter");
  });
  it("кэш — на подключение: другая база грузит свой план счетов", async () => {
    const a = connection();
    const b = connection();
    for (const c of [a, b])
      vi.spyOn(c, "available").mockResolvedValue(new Set(["ChartOfAccounts_Хозрасчетный"]));
    const ra = vi
      .spyOn(a.client, "getCollection")
      .mockResolvedValue({ value: [{ Code: "51", Ref_Key: "x" }] });
    const rb = vi
      .spyOn(b.client, "getCollection")
      .mockResolvedValue({ value: [{ Code: "51", Ref_Key: "y" }] });
    expect((await accountsByCode(a, ["51"])).get("51")).toBe("x");
    expect((await accountsByCode(b, ["51"])).get("51")).toBe("y");
    expect(ra).toHaveBeenCalledOnce();
    expect(rb).toHaveBeenCalledOnce();
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
