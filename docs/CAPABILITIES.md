# Capability map and migration boundaries

The native Node server remains the default installation. Optional Python and documentation tools are source contributions in this repository; `npx 1c-odata-mcp` does not install or start them. This map describes where each capability lives so a deployment can decide which contracts it needs.

| Capability | Native Node server | Optional contribution / remaining setup |
| --- | --- | --- |
| Metadata discovery, typed reads, multi-database access | Existing upstream tools and cached OData metadata | Publish required objects in each source |
| Counterparty/document creation and update | Existing preview, confirmation, per-base write flags and durable operation journal | 1C validates business rules and produces postings |
| Required unique Belarus UNP | Opt-in per-base policy, metadata property mapping, alphanumeric personal UNP, fresh exact duplicate checks, process-local coordination | Configure the field; database-wide uniqueness requires a 1C constraint / coordinated writers; see [Belarus setup](BELARUS.md) |
| Chart of accounts, balances, turnovers and document postings | Configurable chart/register entity names and analytics account prefixes | Published property/dimension/virtual-table contracts must match; live compatibility is configuration-specific |
| Account selection for document preparation | Configurable candidate code groups; nomenclature-register and explicit income/expense overrides | Belarus policy requires explicit verified groups and disables implicit Russian fallback codes |
| Generic migration bridge and durable writes | Native contracts remain supported | [`contrib/python`](../contrib/python/README.md): Python OData client/CSDL handling, payload/reference validation, durable ledger, preview and request contracts |
| Recovery queues, dependency graphs, checkpoints and reconciliation | No native orchestration service added | Optional Python queue planning/checkpoint commands; callers supply source identities, dependencies, state and recovery evidence |
| Bank statement graph and recovery planning | Native cashflow/document reads | Optional Python graph reader, schema, classifier, rule engine and adapters; configure source access and accounting policy |
| Accounting/HR/person/cash workflow helpers | Native document tools where schemas match | Optional Python profile/HR/person/cash helpers; validate destination metadata and business mappings |
| Legacy 1C 7.7 reader | Standard OData targets only | Optional typed legacy bridge and SQLite scan support; a separately installed Windows adapter is required for a live 7.7 source |
| Loopback HTTP bridge / browser-origin tooling | Native MCP uses stdio | Optional bearer-protected Python runtime and platform helpers; install/configure deliberately, no automatic second service |
| Vendor documentation graph, collection and browser workflow | No documentation crawler in the server runtime | [`docs/toolkit`](toolkit/README.md): separate graph/collector/browser tools and licensing/access guidance |

The optional Python distribution is `onec-community-toolkit` (Python 3.11+). Its import namespaces are `onec_odata_mcp`, `agent_bridge`, `onec_mcp_runtime` and `onec_bank_graph`. Its CLIs include `onec-python-mcp`, `onec83-queue-plan`, `onec83-queue-checkpoint`, `onec83-profile-preview`, `onec-legacy-bridge`, `onec-bank-graph` and `onec-bank-rules`; the package README is the source for installation and invocation details.

Python helper payloads and CLI examples target the Python MCP contract. They are not automatically translated to the native Node tool names or schemas. Cross-runtime ledger, preview-token and queue-state compatibility is not claimed. Use a single runtime's documented confirmation/reconciliation flow for each operation, and migrate state explicitly before switching writers. The Python API accepts injected credentials; its convenience credential lookup and some runtime helpers currently target macOS. The Windows legacy adapter remains a separate deployment dependency.

The reusable code does not include organization records, production source bindings, local credentials, operational issue history or private execution state. Deployment configuration, source publication and live validation remain the deployer's work. Neither the offline tests nor presence of a reusable helper proves that a particular accounting configuration is ready to replace an existing production deployment.
