import { describe, expect, it } from "vitest";
import {
  evaluateClosing,
  invoiceBases,
  monthBounds,
  netByTopCode,
  overallStatus,
  wrongSideBalances,
  type BalanceSnapshot,
} from "../src/tools/month-close.js";

const A = (key: string, code: string) => ({ key, code, description: code });

describe("monthBounds", () => {
  it("последний день месяца, включая февраль високосного года", () => {
    expect(monthBounds("2026-09")).toEqual({ from: "2026-09-01", to: "2026-09-30" });
    expect(monthBounds("2028-02")).toEqual({ from: "2028-02-01", to: "2028-02-29" });
  });
});

describe("evaluateClosing", () => {
  const op = (kind: string, state = "Выполнено") => ({ month: "2026-08", kind, state });

  it("закрыт — есть «Закрытие счетов 90, 91» и нет ошибок; «Пропущено» не ошибка", () => {
    const r = evaluateClosing([
      op("ЗакрытиеСчета44"),
      op("НачислениеПроцентов", "Пропущено"),
      op("ЗакрытиеСчетов90_91"),
    ]);
    expect(r.closed).toBe(true);
    expect(r.failed).toEqual([]);
  });

  it("не закрыт — без ключевой операции или с ошибкой в любой", () => {
    expect(evaluateClosing([op("ЗакрытиеСчета44")]).closed).toBe(false);
    const withError = evaluateClosing([
      op("ЗакрытиеСчетов90_91"),
      op("ЗакрытиеСчета44", "ВыполненоСОшибками"),
    ]);
    expect(withError.closed).toBe(false);
    expect(withError.failed.map((f) => f.kind)).toEqual(["ЗакрытиеСчета44"]);
    expect(evaluateClosing([]).closed).toBe(false);
  });
});

describe("overallStatus", () => {
  const c = (status: "ok" | "warning" | "error" | "not_applicable" | "skipped") => ({
    id: status,
    title: status,
    status,
    message: "",
  });
  it("худший статус", () => {
    expect(overallStatus([c("ok"), c("not_applicable"), c("skipped")])).toBe("ok");
    expect(overallStatus([c("ok"), c("warning")])).toBe("warnings");
    expect(overallStatus([c("warning"), c("error")])).toBe("errors");
  });
});

describe("wrongSideBalances", () => {
  const row = (acc: string, dims: string[], dr: number, cr: number, qty = 0) => ({
    Account_Key: acc,
    ...Object.fromEntries(dims.map((d, i) => [`ExtDimension${i + 1}`, d])),
    СуммаBalanceDr: dr,
    СуммаBalanceCr: cr,
    КоличествоBalanceDr: qty > 0 ? qty : 0,
    КоличествоBalanceCr: qty < 0 ? -qty : 0,
  });

  it("сальдо копится по счёту и аналитике: документы одного договора взаимно гасятся", () => {
    const rows = [row("62.01", ["cp", "c1", "doc1"], 100, 0), row("62.01", ["cp", "c1", "doc2"], 0, 150)];
    const red = wrongSideBalances(rows, () => "debit", 2);
    expect(red).toEqual([{ accountKey: "62.01", dims: ["cp", "c1"], cents: -5000 }]);
    // По документам отдельно было бы одно красное; по договору итог тот же — красное −50.
    expect(wrongSideBalances([row("62.01", ["cp", "c1", "d"], 100, 0)], () => "debit", 2)).toEqual([]);
  });

  it("кредитовый счёт (62.02) красный при дебетовом сальдо; товар — при отрицательном количестве", () => {
    expect(wrongSideBalances([row("62.02", ["cp"], 10, 0)], () => "credit", 1)).toHaveLength(1);
    const stock = wrongSideBalances([row("10.06", ["item", "wh"], 0, 0, -1)], () => "debit", 2, true);
    expect(stock).toEqual([{ accountKey: "10.06", dims: ["item", "wh"], cents: 0, quantity: -1 }]);
  });

  it("счета вне проверки пропускаются", () => {
    expect(wrongSideBalances([row("x", ["cp"], 0, 10)], () => undefined, 1)).toEqual([]);
  });
});

describe("netByTopCode", () => {
  it("чистое сальдо по группе счетов с исключениями", () => {
    const snap: BalanceSnapshot = {
      byKey: new Map([
        ["k1", A("k1", "90.01.1")],
        ["k2", A("k2", "90.09")],
        ["k3", A("k3", "01.01")],
        ["k4", A("k4", "01.09")],
      ]),
      rows: [
        { Account_Key: "k1", СуммаBalanceDr: 0, СуммаBalanceCr: 1000 },
        { Account_Key: "k2", СуммаBalanceDr: 1000, СуммаBalanceCr: 0 },
        { Account_Key: "k3", СуммаBalanceDr: 500, СуммаBalanceCr: 0 },
        { Account_Key: "k4", СуммаBalanceDr: 70, СуммаBalanceCr: 0 },
      ],
    };
    expect(netByTopCode(snap, ["90"]).get("90")).toBe(0);
    expect(netByTopCode(snap, ["01"], (a) => a.code.startsWith("01.09")).get("01")).toBe(50000);
  });
});

describe("invoiceBases", () => {
  it("основание из шапки и из табличной части", () => {
    const sf = {
      ДокументОснование: "11111111-1111-1111-1111-111111111111",
      ДокументыОснования: [
        { ДокументОснование: "22222222-2222-2222-2222-222222222222" },
        { ДокументОснование: "" },
      ],
    };
    expect(invoiceBases(sf)).toEqual([
      "11111111-1111-1111-1111-111111111111",
      "22222222-2222-2222-2222-222222222222",
    ]);
  });
});
