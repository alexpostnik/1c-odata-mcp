import { describe, it, expect } from "vitest";
import { fromHttpStatus, ODataError } from "../src/odata/errors.js";
import { InputError } from "../src/errors.js";
import { guard, ok } from "../src/tools/_shared.js";

const textOf = (r: { content: Array<{ type: string; text?: string }> }): string =>
  r.content.map((c) => c.text ?? "").join("\n");

describe("fromHttpStatus — классификация HTTP-ошибок OData", () => {
  it("401 → auth, не повторяется", () => {
    const e = fromHttpStatus(401, "url");
    expect(e.kind).toBe("auth");
    expect(e.retryable).toBe(false);
  });
  it("404 → not_found", () => {
    expect(fromHttpStatus(404, "url").kind).toBe("not_found");
  });
  it("400 → bad_request", () => {
    expect(fromHttpStatus(400, "url").kind).toBe("bad_request");
  });
  it("429 → rate_limit, повторяется", () => {
    const e = fromHttpStatus(429, "url");
    expect(e.kind).toBe("rate_limit");
    expect(e.retryable).toBe(true);
  });
  it("500 → server, повторяется", () => {
    const e = fromHttpStatus(500, "url");
    expect(e.kind).toBe("server");
    expect(e.retryable).toBe(true);
  });
  it("вытаскивает сообщение 1С из JSON-тела ошибки", () => {
    const body = JSON.stringify({ error: { message: { value: "Плохой фильтр" } } });
    expect(fromHttpStatus(400, "url", body).message).toContain("Плохой фильтр");
  });
  it("вытаскивает сообщение из ключа odata.error (OData v3, реальный формат 1С)", () => {
    const body = JSON.stringify({
      "odata.error": {
        code: "-1",
        message: { lang: "ru", value: "Не удалось записать: Счет-фактура выданный!" },
      },
    });
    expect(fromHttpStatus(500, "url", body).message).toContain("Не удалось записать");
  });
  // Тексты ответов 1С сняты с живой базы (БП 3.0).
  const odata404 = (value: string) =>
    JSON.stringify({ "odata.error": { code: "-1", message: { lang: "ru", value } } });
  it("404 «Сущность не найдена» — набор не опубликован: подсказка про «Состав»", () => {
    const e = fromHttpStatus(404, "url", odata404("Сущность 'InformationRegister_Х' не найдена"));
    expect(e.kind).toBe("not_found");
    expect(e.message).toContain("Состав");
  });
  it("404 «Экземпляр сущности не найден» — записи нет: без подсказки про «Состав»", () => {
    const e = fromHttpStatus(404, "url", odata404("Экземпляр сущности не найден"));
    expect(e.kind).toBe("not_found");
    expect(e.message).toContain("записи с таким ключом нет");
    expect(e.message).not.toContain("Состав");
  });
});

describe("guard — ошибка ввода отделена от сбоя сервера", () => {
  it("InputError отдаётся текстом как есть, без «Внутренняя ошибка инструмента»", async () => {
    const r = await guard("write.sales.create_act", () => {
      throw new InputError("Счёт «90.01.9» не найден в плане счетов «Хозрасчётный».");
    });
    expect(r.isError).toBe(true);
    expect(textOf(r)).toBe("Счёт «90.01.9» не найден в плане счетов «Хозрасчётный».");
    expect(textOf(r)).not.toContain("Внутренняя ошибка");
  });

  it("ODataError по-прежнему помечается видом сбоя", async () => {
    const r = await guard("read.analytics.get_sales", () => {
      throw new ODataError({ kind: "auth", message: "Ошибка авторизации" });
    });
    expect(textOf(r)).toBe("[auth] Ошибка авторизации");
  });

  it("непредвиденная ошибка остаётся внутренней — это поломка, а не ввод", async () => {
    const r = await guard("read.analytics.get_sales", () => {
      throw new TypeError("x is not a function");
    });
    expect(textOf(r)).toContain("Внутренняя ошибка инструмента read.analytics.get_sales");
  });

  it("успешный вызов проходит насквозь", async () => {
    const r = await guard("read.system.health_check", () => Promise.resolve(ok({ status: "ok" })));
    expect(r.isError).toBeUndefined();
  });
});
