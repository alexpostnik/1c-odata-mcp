import { describe, expect, it, vi } from "vitest";
import { cached, lookupWithRefresh, REFERENCE_TTL_MS, REFRESH_MIN_AGE_MS } from "../src/odata/cache.js";
import { resolveOrganization } from "../src/odata/orgs.js";
import type { Connection } from "../src/context.js";

describe("cached", () => {
  it("одна загрузка на параллельные вызовы; повтор — до истечения срока", async () => {
    const owner = {};
    let t = 0;
    const load = vi.fn(async () => "v");
    await Promise.all([cached(owner, "k", load, 1000, () => t), cached(owner, "k", load, 1000, () => t)]);
    t = 999;
    await cached(owner, "k", load, 1000, () => t);
    expect(load).toHaveBeenCalledOnce();
    t = 1000;
    await cached(owner, "k", load, 1000, () => t);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("ошибка не кэшируется", async () => {
    const owner = {};
    const load = vi.fn().mockRejectedValueOnce(new Error("сеть")).mockResolvedValue("ok");
    await expect(cached(owner, "k", load)).rejects.toThrow("сеть");
    await expect(cached(owner, "k", load)).resolves.toBe("ok");
    expect(load).toHaveBeenCalledTimes(2);
  });
});

describe("lookupWithRefresh", () => {
  it("промах в свежем кэше — настоящий; в постаревшем — одно перечитывание", async () => {
    const owner = {};
    let t = 0;
    const lists = [["a"], ["a", "b"]];
    const load = vi.fn(async () => lists[load.mock.calls.length - 1]!);
    const find = (x: string) =>
      lookupWithRefresh(
        owner,
        "k",
        load,
        (l: string[]) => l.includes(x),
        (r) => !r,
        () => t,
      );
    expect(await find("b")).toBe(false); // кэш свежий — не перечитываем
    expect(load).toHaveBeenCalledOnce();
    t = REFRESH_MIN_AGE_MS;
    expect(await find("b")).toBe(true); // постарел — перечитали и нашли
    expect(load).toHaveBeenCalledTimes(2);
    expect(REFERENCE_TTL_MS).toBeGreaterThan(REFRESH_MIN_AGE_MS);
  });
});

describe("resolveOrganization", () => {
  const ORG1 = "11111111-1111-4111-8111-111111111111";
  const conn = () => {
    const getCollection = vi.fn(async () => ({
      value: [
        { Ref_Key: ORG1, Description: "ООО Ромашка", ИНН: "7701234567" },
        { Ref_Key: "22222222-2222-4222-8222-222222222222", Description: "ООО Ромашка-Юг", ИНН: "7707654321" },
      ],
    }));
    return {
      c: {
        client: { getCollection },
        behavior: { pageSize: 100, maxRows: 1000 },
        available: async () => new Set(["Catalog_Организации"]),
      } as unknown as Connection,
      getCollection,
    };
  };

  it("по ссылке, ИНН и точному названию; список грузится один раз", async () => {
    const { c, getCollection } = conn();
    expect((await resolveOrganization(c, ORG1.toUpperCase())).name).toBe("ООО Ромашка");
    expect((await resolveOrganization(c, "7707654321")).name).toBe("ООО Ромашка-Юг");
    expect((await resolveOrganization(c, "ооо ромашка")).ref).toBe(ORG1);
    expect(getCollection).toHaveBeenCalledOnce();
  });

  it("неоднозначная часть названия и отсутствующая организация — понятные ошибки", async () => {
    const { c } = conn();
    await expect(resolveOrganization(c, "Ромаш")).rejects.toThrow("несколько организаций");
    await expect(resolveOrganization(c, "Берёзка")).rejects.toThrow("не найдена");
  });
});
