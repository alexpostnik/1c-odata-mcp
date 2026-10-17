# Belarus counterparty and accounting configuration

The native Node server supports an explicit Belarus policy for counterparty identity and configurable accounting mappings. It keeps the existing Russian configuration when no policy is selected. These settings do not install a Belarus configuration in 1C or change its posting algorithms.

## Required unique UNP

Set these variables in the server's environment (or in `.env` when using the documented env-file launch):

```dotenv
ODATA_COUNTERPARTY_TAX_POLICY=by-required-unique
# Set this only after checking read.system.describe_entity / $metadata:
ODATA_COUNTERPARTY_TAX_FIELD=УНП
```

For multiple databases, the corresponding overrides are `ODATA_DB_<NAME>_COUNTERPARTY_TAX_POLICY` and `ODATA_DB_<NAME>_COUNTERPARTY_TAX_FIELD`. Unspecified per-database settings inherit the global settings. Explicitly set `ru` on databases that should retain the original behavior.

The field must be a published `Edm.String` property of `Catalog_Контрагенты`. Without an explicit mapping, the server accepts a single published candidate among `УНП` and `ИНН`. If both exist, neither exists, or the chosen field is not a string, it stops and asks for a mapping. It does not add or guess fields.

The `inn` argument of `write.counterparty.create_counterparty` and `write.counterparty.update_counterparty` carries the UNP under this policy. The server maps it to the configured field. Counterparty reads return the identifier in the existing `inn` output property; search accepts UNP or a name. Generic entity tools use the actual metadata property name in their payload.

For a counterparty card, UNP must be a nonblank alphanumeric string supplied by the caller. Personal identifiers containing letters are accepted. Leading/trailing whitespace, punctuation, all-zero values and common placeholder words are rejected. No checksum, official registration, residency or legal status is inferred. Structural folders with boolean `IsFolder=true` are exempt; converting an existing card into a folder cannot bypass the policy.

Both preview and the actual write enforce the policy, including generic updates and creates. An update that omits UNP reads and validates the existing value; it cannot clear the field. Duplicate detection includes deletion-marked cards. The normal check is one server-side exact `eq` query with `$top=2` and a narrow `$select`. The server refuses writes on query errors, malformed or contradictory responses, or continuation links. There is no expensive catalog-scan fallback. A backend that cannot support this exact filter needs an adapter or configuration change before writes can proceed.

The final check runs inside the transport immediately before sending POST/PATCH, after preview/journal/reference work. A refusal before transport is recorded as rejected; no write was sent. Successful operation replay returns from the existing durable journal without another uniqueness lookup or POST. Lost responses keep the existing unknown-outcome/reconciliation flow.

Creates to the same normalized base URL are serialized within one Node process. An uncertain create also reserves that UNP in memory until the operation is reconciled or explicitly marked not applied through the existing status flow. This protects cooperating connections in that process. It cannot enforce uniqueness against another Node process, URL aliases resolving to the same database, unrelated 1C clients, or an earlier request that survives a process restart. For a database-wide guarantee, enforce the same constraint in the 1C configuration and coordinate writers. Preserve the write journal and reconcile uncertain operations before restarting or switching writers.

## Accounting mappings

`ODATA_ACCOUNTING_CONFIG` is a JSON object; `ODATA_DB_<NAME>_ACCOUNTING_CONFIG` replaces it for a specific database. The supported keys are:

| Key | Meaning |
| --- | --- |
| `chart` | Published `ChartOfAccounts_*` entity set for code/ref lookup |
| `register` | Published `AccountingRegister_*` used for balances, turnovers and postings |
| `prefixes.receivables` | Account code prefixes for debtors |
| `prefixes.inventory` | Account code prefixes for stock analytics |
| `defaults.goods` | Candidate goods account codes, in priority order |
| `defaults.incomingVat`, `defaults.outgoingVat` | Candidate input/output VAT account codes |
| `defaults.income`, `defaults.expense` | Candidate income/expense account codes |
| `defaults.receivable`, `defaults.receivableAdvance` | Candidate customer settlement/advance codes |
| `defaults.payable`, `defaults.payableAdvance` | Candidate supplier settlement/advance codes |

Each prefix/default group is a nonempty array of strings. Unknown keys, invalid entity prefixes and malformed JSON are rejected at startup. Copy the technical entity names and account codes from your own metadata/chart. The following is a **template**, not a Belarus chart recommendation; replace every `REPLACE_*` value before use:

```json
{
  "chart": "ChartOfAccounts_REPLACE_FROM_METADATA",
  "register": "AccountingRegister_REPLACE_FROM_METADATA",
  "prefixes": {
    "receivables": ["REPLACE_CUSTOMER_PREFIX"],
    "inventory": ["REPLACE_INVENTORY_PREFIX"]
  },
  "defaults": {
    "goods": ["REPLACE_GOODS_CODE"],
    "incomingVat": ["REPLACE_INPUT_VAT_CODE"],
    "outgoingVat": ["REPLACE_OUTPUT_VAT_CODE"],
    "income": ["REPLACE_INCOME_CODE"],
    "expense": ["REPLACE_EXPENSE_CODE"],
    "receivable": ["REPLACE_CUSTOMER_CODE"],
    "receivableAdvance": ["REPLACE_CUSTOMER_ADVANCE_CODE"],
    "payable": ["REPLACE_SUPPLIER_CODE"],
    "payableAdvance": ["REPLACE_SUPPLIER_ADVANCE_CODE"]
  }
}
```

Serialize this object onto one line for the environment variable. With no overrides, the chart/register candidate remains `Хозрасчетный`, subject to publication checks. Under `by-required-unique`, Russian default account codes and analytics prefixes are disabled: a tool requiring an unconfigured group fails with the group's name, and a configured fallback group with no matching published account also fails. Configure the groups your intended document tools use before confirming a write.

Code candidates are resolved in a single batched read per document preparation, with duplicate codes removed. Existing nomenclature-register lookup and explicit line/document income/expense overrides retain their precedence. Configuring chart/register names does not translate document types, field names, enum values, VAT rules, dimensions or virtual-table contracts. Those structures must match the tool or be adapted explicitly. Generic metadata/read/write tools and the optional Python toolkit are available where a configuration needs a different contract; see [the capability map](CAPABILITIES.md).

## Offline verification

Run `npm ci`, `npm run typecheck`, `npm run lint`, `npm run build` and `npm test`. `test/belarus.test.ts` uses synthetic metadata and mocked transport with the real write journal; it checks UNP mapping, duplicate/refusal behavior, confirmation races, retries and bounded query counts. `npm run test:coverage -- --coverage.include=src/config/accounting.ts --coverage.include=src/odata/counterparty-policy.ts` measures the new modules. These tests establish client behavior without claiming compatibility with a live Belarus database. No live 1C calls are needed for the suite.
