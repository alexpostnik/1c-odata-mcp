import { describe, expect, it } from "vitest";
import type { CardEntry } from "../src/odata/account-card.js";
import { originByDocuments, persistentOrigin } from "../src/tools/card.js";

const e = (period: string, side: "debit" | "credit", rub: number, recorder = period): CardEntry => ({
  period,
  side,
  cents: Math.round(rub * 100),
  account: "acc",
  dims: [],
  corrDims: [],
  recorder,
});

describe("persistentOrigin", () => {
  it("старый хвост при ежемесячных оборотах: виноват документ, после которого остаток не уходил в ноль", () => {
    const entries = [
      e("2024-12-24", "debit", -458, "ретро-сторно"), // сторно дебета: остаток −458
      e("2025-01-10", "credit", 100, "отчёт-1"),
      e("2025-01-20", "debit", 100, "оплата-1"),
      e("2025-02-10", "credit", 200, "отчёт-2"),
      e("2025-02-20", "debit", 200, "оплата-2"),
    ];
    const o = persistentOrigin(entries, -45800);
    expect(o.found).toBe(true);
    expect(o.items.map((x) => [x.e.recorder, x.cents])).toEqual([["ретро-сторно", 45800]]);
  });

  it("остаток из нескольких документов: доли в сумме = остатку", () => {
    const entries = [
      e("2026-07-01", "debit", 100, "a"),
      e("2026-07-10", "credit", 100, "close-june"),
      e("2026-07-20", "debit", 50, "b"),
      e("2026-07-25", "debit", 30, "c"),
      e("2026-07-31", "credit", 40, "close-july"),
    ];
    const o = persistentOrigin(entries, 4000);
    expect(o.found).toBe(true);
    expect(o.items.reduce((s, x) => s + x.cents, 0)).toBe(4000);
    expect(o.items.map((x) => x.e.recorder)).toEqual(["b"]);
  });

  it("момент появления раньше переданных движений — часть остатка «старше поиска»", () => {
    const o = persistentOrigin([e("2026-08-01", "debit", 10, "x")], 5000);
    expect(o.found).toBe(false);
    expect(o.carried).toBe(4000);
    expect(o.items.map((x) => [x.e.recorder, x.cents])).toEqual([["x", 1000]]);
  });
});

describe("originByDocuments", () => {
  const row = (doc: string | null, dr: number, cr: number) => ({
    ExtDimension1: "cp",
    ExtDimension1_Type: "StandardODATA.Catalog_Контрагенты",
    ExtDimension2: doc,
    ExtDimension2_Type: doc ? "StandardODATA.Document_РеализацияТоваровУслуг" : "",
    СуммаBalanceDr: dr,
    СуммаBalanceCr: cr,
  });
  it("точный состав, если у всех строк есть документ расчётов", () => {
    const r = originByDocuments([row("d1", 100, 0), row("d2", 0, 30)], ["cp"]);
    expect(r.complete).toBe(true);
    expect(r.items.map((x) => [x.doc.ref, x.cents])).toEqual([
      ["d1", 10000],
      ["d2", -3000],
    ]);
  });
  it("строка без документа — неполный состав", () => {
    expect(originByDocuments([row("d1", 100, 0), row(null, 5, 0)], ["cp"]).complete).toBe(false);
  });
});
