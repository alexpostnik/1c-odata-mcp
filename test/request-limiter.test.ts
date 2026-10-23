import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ODataError } from "../src/odata/errors.js";
import { RequestLimiter } from "../src/odata/limiter.js";
import { WriteOperationJournal } from "../src/odata/write-journal.js";
import { fake1C } from "./helpers/fake-1c.js";

// #57: необязательный предел одновременных HTTP одного подключения.
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** fetch с задержкой, считающий пик одновременных запросов (включая чтение тела). */
function delayedFetch(ms: number, status = 200) {
  const stats = { active: 0, peak: 0, calls: 0 };
  const fetchMock = vi.fn(async (input: string | URL) => {
    stats.calls += 1;
    stats.active += 1;
    stats.peak = Math.max(stats.peak, stats.active);
    await sleep(ms);
    const url = String(input);
    const body = url.includes("$metadata")
      ? "<edmx:Edmx/>"
      : status === 200
        ? JSON.stringify({ value: [] })
        : JSON.stringify({ "odata.error": { message: { value: "Сеанс занят" } } });
    // Тело «читается» тоже с задержкой — место должно держаться и на это время.
    return {
      ok: status === 200,
      status,
      headers: new Headers(),
      text: async () => {
        await sleep(ms);
        stats.active -= 1;
        return body;
      },
    } as unknown as Response;
  });
  return { fetchMock, stats };
}

describe("RequestLimiter", () => {
  it("limit=0 — без ограничений", async () => {
    const l = new RequestLimiter(0, 10);
    const releases = await Promise.all(Array.from({ length: 10 }, () => l.acquire()));
    releases.forEach((r) => r());
    expect(l.inFlight).toBe(0);
  });

  it("FIFO: место передаётся первому ожидающему; повторное освобождение безопасно", async () => {
    const l = new RequestLimiter(1, 1000);
    const first = await l.acquire();
    const order: number[] = [];
    const a = l.acquire().then((r) => (order.push(1), r));
    const b = l.acquire().then((r) => (order.push(2), r));
    first();
    first(); // идемпотентно: второе место не освобождает
    const ra = await a;
    expect(order).toEqual([1]);
    expect(l.inFlight).toBe(1);
    ra();
    (await b)();
    expect(order).toEqual([1, 2]);
    expect(l.inFlight).toBe(0);
  });

  it("не дождался места — ошибка busy, запрос не отправлялся; очередь не застревает", async () => {
    const l = new RequestLimiter(1, 30);
    const held = await l.acquire();
    const err = await l.acquire().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ODataError);
    expect((err as ODataError).kind).toBe("busy");
    expect((err as ODataError).retryable).toBe(false);
    expect((err as ODataError).message).toContain("не отправлялся");
    held();
    // Ожидавший по тайм-ауту ушёл из очереди — следующее место свободно сразу.
    const again = await l.acquire();
    expect(l.inFlight).toBe(1);
    again();
  });
});

describe("ODataClient с ODATA_MAX_CONCURRENT_REQUESTS", () => {
  it("смешанные чтения соблюдают пик 2, все выполняются", async () => {
    const { conn } = fake1C({ ODATA_MAX_CONCURRENT_REQUESTS: "2" });
    const { fetchMock, stats } = delayedFetch(15);
    vi.stubGlobal("fetch", fetchMock);
    await Promise.all([
      ...Array.from({ length: 5 }, (_, i) => conn.client.getCollection(`Catalog_X${i}?$format=json`)),
      conn.client.getText("$metadata"),
    ]);
    expect(stats.calls).toBe(6);
    expect(stats.peak).toBe(2);
    expect(conn.client.limiter.inFlight).toBe(0);
  });

  it("по умолчанию предела нет — поведение как раньше", async () => {
    const { conn } = fake1C();
    const { fetchMock, stats } = delayedFetch(15);
    vi.stubGlobal("fetch", fetchMock);
    await Promise.all(
      Array.from({ length: 6 }, (_, i) => conn.client.getCollection(`Catalog_X${i}?$format=json`)),
    );
    expect(stats.peak).toBe(6);
  });

  it("ошибка и тайм-аут не теряют место", async () => {
    const { conn } = fake1C({
      ODATA_MAX_CONCURRENT_REQUESTS: "1",
      ODATA_RETRIES: "0",
      ODATA_TIMEOUT_MS: "20",
    });
    vi.stubGlobal("fetch", delayedFetch(5, 500).fetchMock);
    await expect(conn.client.getCollection("Catalog_X?$format=json")).rejects.toBeInstanceOf(ODataError);
    expect(conn.client.limiter.inFlight).toBe(0);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_: unknown, init: RequestInit) =>
          new Promise((_resolve, reject) =>
            init.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
          ),
      ),
    );
    await expect(conn.client.getCollection("Catalog_X?$format=json")).rejects.toBeInstanceOf(ODataError);
    expect(conn.client.limiter.inFlight).toBe(0);
  });
});

describe("запись, не дождавшаяся места, — исход известен (#57)", () => {
  const roots: string[] = [];
  afterEach(async () => {
    await Promise.all(roots.splice(0).map((r) => rm(r, { recursive: true, force: true })));
  });
  it("busy в журнале — rejected, а не outcome_unknown; POST не отправлялся", async () => {
    const root = await mkdtemp(join(tmpdir(), "1c-odata-limiter-"));
    roots.push(root);
    const journal = new WriteOperationJournal(root, "buh", "https://1c.example/odata/");
    const id = randomUUID();
    const payload = { Description: "x" };
    await journal.prepare(id, "Catalog_X", payload, "h");
    const limiter = new RequestLimiter(1, 10);
    const held = await limiter.acquire();
    let sent = 0;
    const err = await journal
      .execute(id, "Catalog_X", payload, "h", async () => {
        const release = await limiter.acquire();
        sent += 1;
        release();
        return { Ref_Key: "r" };
      })
      .catch((e: unknown) => e);
    held();
    expect(sent).toBe(0);
    expect((err as ODataError).kind).toBe("busy");
    expect((await journal.lookup(id))?.state).toBe("rejected");
  });
});
