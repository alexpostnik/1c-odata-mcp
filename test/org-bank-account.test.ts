import { describe, expect, it, vi } from "vitest";
import { CATALOGS } from "../src/config/mapping.js";
import type { Connection } from "../src/context.js";
import { ODataError } from "../src/odata/errors.js";
import { resolveOrgBankAccount } from "../src/tools/write.js";

// #54: подбор банковского счёта организации по названию. 1С не даёт фильтровать по Owner, поэтому
// счета всех организаций читаются ограниченным списком и фильтруются на клиенте.
const ORG = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const ref = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
const account = (
  i: number,
  owner: string,
  name: string,
  number = `4070281000000000${String(i).padStart(4, "0")}`,
) => ({
  Ref_Key: ref(i),
  Description: name,
  НомерСчета: number,
  Owner: owner,
});

function connection(rows: Array<Record<string, unknown>> | Error) {
  const getCollection = vi.fn(async (path: string) => {
    if (rows instanceof Error) throw rows;
    const q = new URLSearchParams(path.slice(path.indexOf("?") + 1));
    const top = Number(q.get("$top")),
      skip = Number(q.get("$skip") ?? 0);
    return { value: rows.slice(skip, skip + top) };
  });
  const conn = {
    available: async () => new Set([CATALOGS.bankAccounts[0]!, CATALOGS.organizations[0]!]),
    behavior: { pageSize: 100 },
    client: {
      getCollection,
      // основного счёта нет
      getEntity: async () => ({}),
    },
  } as unknown as Connection;
  return { conn, getCollection };
}

describe("resolveOrgBankAccount (#54)", () => {
  it("находит счёт организации за пределами прежних 200 строк", async () => {
    const rows = [
      ...Array.from({ length: 200 }, (_, i) => account(i, OTHER, `Чужой ${i}`)),
      account(500, ORG, "Сбербанк"),
    ];
    const { conn } = connection(rows);
    await expect(resolveOrgBankAccount(conn, ORG, "Сбербанк")).resolves.toBe(ref(500));
  });

  it("список обрезан и совпадения по названию нет — не «не найден», а просьба указать номер или Ref_Key", async () => {
    const rows = [
      ...Array.from({ length: 1000 }, (_, i) => account(i, OTHER, `Чужой ${i}`)),
      account(5000, ORG, "Сбербанк"),
    ];
    const { conn } = connection(rows);
    const err = await resolveOrgBankAccount(conn, ORG, "Сбербанк").catch((e: Error) => e);
    expect(String(err)).toContain("прочитана только часть");
    expect(String(err)).not.toContain("не найден.");
  });

  it("список обрезан, но счёт указан точным номером из прочитанной части — принимается", async () => {
    const rows = [
      account(1, ORG, "Сбербанк", "40702810000000000001"),
      ...Array.from({ length: 1000 }, (_, i) => account(10 + i, OTHER, `Чужой ${i}`)),
    ];
    const { conn } = connection(rows);
    await expect(resolveOrgBankAccount(conn, ORG, "40702810000000000001")).resolves.toBe(ref(1));
  });

  it("ошибка чтения не превращается в «счёт не найден»", async () => {
    const { conn } = connection(new ODataError({ kind: "network", message: "Сеть недоступна" }));
    const err = await resolveOrgBankAccount(conn, ORG, "Сбербанк").catch((e: Error) => e);
    expect(String(err)).toContain("Сеть недоступна");
    expect(String(err)).not.toContain("не найден");
  });

  it("без названия и при обрезанном списке не подставляет случайный «первый» счёт", async () => {
    const rows = [
      account(1, ORG, "Альфа"),
      ...Array.from({ length: 1000 }, (_, i) => account(10 + i, OTHER, `Чужой ${i}`)),
    ];
    const { conn } = connection(rows);
    await expect(resolveOrgBankAccount(conn, ORG, undefined)).resolves.toBeUndefined();
  });

  it("без названия и полный список — первый по алфавиту, как раньше", async () => {
    const { conn } = connection([
      account(2, ORG, "Тинькофф"),
      account(1, ORG, "Альфа"),
      account(3, OTHER, "Аааа"),
    ]);
    await expect(resolveOrgBankAccount(conn, ORG, undefined)).resolves.toBe(ref(1));
  });

  it("полный список и счёта нет — прежнее «не найден» со списком имеющихся", async () => {
    const { conn } = connection([account(1, ORG, "Альфа")]);
    const err = await resolveOrgBankAccount(conn, ORG, "Сбербанк").catch((e: Error) => e);
    expect(String(err)).toContain("не найден");
    expect(String(err)).toContain("Альфа");
  });
});
