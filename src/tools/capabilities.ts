import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { ServerContext } from "../context.js";
import { REQUIREMENTS } from "../capabilities/requirements.js";
import { diagnoseDynamic } from "../capabilities/diagnose.js";
import { databaseField, guard, ok } from "./_shared.js";

const requirementsOutput = z
  .object({
    schemaVersion: z.number(),
    revision: z.string(),
    tools: z.array(z.object({ name: z.string() }).passthrough()),
  })
  .passthrough();
const capabilitiesOutput = z
  .object({
    requirementsRevision: z.string(),
    scope: z.string(),
    summary: z.object({ metadataStatus: z.string(), persistence: z.string() }).passthrough(),
    requirements: z.record(z.string(), z.unknown()),
    events: z.array(z.unknown()),
    administratorRequest: z.string(),
  })
  .passthrough();

const technicalName = z.string().regex(/^[A-Za-z\p{L}_][\p{L}\p{N}_]{0,180}$/u);
export function registerCapabilityTools(server: McpServer, ctx: ServerContext): void {
  server.registerTool(
    "read.system.odata_requirements",
    {
      title: "Требования инструментов к OData",
      outputSchema: requirementsOutput,
      description:
        "Версионный перечень объектов, полей, ключей, функций и операций каждого инструмента. Условные зависимости перечислены отдельно. Для динамического инструмента можно указать точный entitySet и поля без значений записей.",
      inputSchema: {
        database: databaseField,
        tool: z.string().optional(),
        entitySet: technicalName.optional(),
        fields: z.array(technicalName).max(100).optional(),
      },
    },
    async ({ database, tool, entitySet, fields }) =>
      guard("read.system.odata_requirements", async () => {
        const selected = tool ? REQUIREMENTS.tools.filter((t) => t.name === tool) : REQUIREMENTS.tools;
        if (tool && !selected.length)
          throw new Error("Неизвестный инструмент; см. перечень без фильтра tool.");
        const conn = ctx.db(database);
        const dynamic = entitySet
          ? diagnoseDynamic(selected[0], conn.cfg, await conn.getMetadata(), entitySet, fields)
          : undefined;
        return ok({
          ...REQUIREMENTS,
          tools: selected,
          ...(dynamic
            ? {
                dynamic: {
                  ...dynamic,
                  rights: "unknown",
                  note: "Проверены лишь заданные поля схемы; значения/ключи записей не принимаются.",
                },
              }
            : {}),
        });
      }),
  );
  server.registerTool(
    "read.system.odata_capabilities",
    {
      title: "Возможности и изменения OData",
      outputSchema: capabilitiesOutput,
      description:
        "Последняя диагностика по базе и пользователю: схема, наблюдённые операции с ограниченным сроком действия, неизвестные права, изменения и точный запрос администратору. Без новых обращений к 1С.",
      inputSchema: { database: databaseField },
    },
    async ({ database }) =>
      guard("read.system.odata_capabilities", async () => ok(await ctx.db(database).capabilities.status())),
  );
  server.registerTool(
    "read.system.odata_refresh",
    {
      title: "Перепроверить возможности OData",
      outputSchema: capabilitiesOutput,
      description:
        "Принудительно перечитывает metadata, выполняет ограниченный проход GET top=0 по статическим требованиям и сохраняет диагностику. Ни одной пробной записи. Один отказ не прекращает независимые проверки.",
      inputSchema: { database: databaseField },
    },
    async ({ database }) =>
      guard("read.system.odata_refresh", async () =>
        ok(await ctx.db(database).capabilities.refresh(undefined, true)),
      ),
  );
}
