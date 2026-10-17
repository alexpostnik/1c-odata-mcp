# Discover settlement-account settings without guessing their storage

A configuration may store settlement-account settings in a separate information register, keyed by organization, counterparty, contract and settlement kind. Such a setting is distinct from a contract attribute and from a counterparty's bank-account number. Its exact technical name, dimensions, resources, selection precedence and accounting analytics depend on the configuration.

`read.schema.discover_settlement_settings` inspects only the published metadata. It does not read register records or create/update settings. It lists information-register candidates with their exact EDM keys, property names/types/nullability and navigation targets. A search matches actual technical names and property names. Candidate output is bounded; `truncated=true` means the list is incomplete.

Start with:

```json
{"database":"example","limit":50}
```

The tool intentionally does not select a register automatically. Narrow the candidates with `search`, inspect an entity through `read.schema.describe_entity`, and check the relevant configuration documentation. EDM keys are reported as keys; they are not automatically labelled as business dimensions. Other fields are not automatically labelled as resources.

If nothing suitable is visible, the status is `not_published_or_different_name_or_unknown`. Possible explanations include publication scope, another technical name, search scope or unknown visibility. This is not evidence that the 1C function does not exist. A publication/runtime change needs a separate supported deployment action; this diagnostic performs none.

## Explicit mapping: synthetic example

The following names are **synthetic test fixtures**, not a recommended register or field naming convention. Replace all names with verified metadata from your configuration:

```json
{
  "database": "example",
  "mapping": {
    "entitySet": "InformationRegister_SyntheticSettings",
    "dimensions": {
      "organization": "Org_Key",
      "counterparty": "Party_Key",
      "contract": "Agreement_Key",
      "settlementKind": "Kind_Key"
    },
    "resources": {
      "settlementAccount": "Ledger_Key",
      "advanceAccount": "Advance_Key"
    }
  }
}
```

`mapping_fields_verified` means the entity is a published information register, the mapped fields exist, and the mapping uses distinct fields. Their declared EDM types are returned exactly. The business roles remain `caller_supplied_unverified`: a Guid type alone does not prove that a property references a chart of accounts, contract or organization. Confirm that through actual navigation metadata and configuration documentation before using the mapping.

A bank-account number cannot establish a ledger account or posting rule. A contract cannot establish that an account uses contract subkonto. Do not invent missing analytics. Determine account selection priority and required dimensions from the configuration and verified existing settings.

## Access and validation boundary

The native generic update tool uses `Ref_Key`; information-register records may use compound keys instead. This diagnostic reports `genericWriteSupport=unknown`. It neither establishes a supported record read/write path nor fills account settings. The optional Python toolkit also does not currently implement compound-key information-register writes. See [the capability map](CAPABILITIES.md).

After establishing a supported access path, use a separately authorized workflow to read an existing setting, prepare an explicitly mapped change, apply it, and read it back. Validate any document autofill only within that document's authorized workflow. Discovering a schema does not authorize creating or posting a payment document for testing.

`test/settlement-settings.test.ts` covers synthetic metadata, unknown roles, explicit/missing/reused mappings, missing publication, bounded results and zero register-data/write calls. It provides offline evidence of the diagnostic's behavior; live metadata and configuration behavior remain unverified.
