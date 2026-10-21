import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { ServerContext } from "../context.js";
import { z } from "zod";
import { applyBankSettlementSettings, bankSettlementInput } from "../odata/existing-bank-settlement.js";
import { databaseField, guard, ok } from "./_shared.js";
export function registerExistingBankSettlementTools(server: McpServer, ctx: ServerContext): void {
  server.registerTool(
    "write.document.apply_bank_settlement_settings",
    {
      title: "Применить счета расчётов к существующему банковскому документу",
      description:
        "Применяет унаследованный счёт расчётов и выбранный договор к существующему НЕПРОВЕДЁННОМУ банковскому документу с точной expectedDataVersion/If-Match. Явная сторона supplier/customer обязательна, включая договор Прочее. Сохраняет остальные поля шапки и единственной строки платежа, включая статью затрат; несколько строк отклоняются. Предпросмотр связан с payload, документом и результатом подбора. Отмена проведения и проведение выполняются отдельными вызовами; unknown удерживается без повторной записи.",
      inputSchema: { database: databaseField, ...bankSettlementInput, confirm: z.boolean().default(false) },
      outputSchema: z.object({}).passthrough(),
    },
    (args) =>
      guard("write.document.apply_bank_settlement_settings", async () =>
        ok(await applyBankSettlementSettings(ctx.db(args.database), args, args.confirm)),
      ),
  );
}
