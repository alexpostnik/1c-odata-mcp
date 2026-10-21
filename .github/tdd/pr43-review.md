# PR #43 review corrections

Source plan: review 5602915426 against original candidate 94c7f02, the upstream CONTRIBUTING contract and BP 3.0 metadata snapshot at c37b397. Reused the existing journal, resolver, and metadata helpers. Belarus schema fixture contains only fields, types, and keys from targeted read-only TEST observations on 2026-10-21 (configuration/platform versions supplied by the owner).

The user journeys are: apply saved accounts to existing bank documents under either published counterparty representation; recover a lost group-creation reply without duplicate advice; retain old creation lookup behavior and call count; keep version and uncertain-write guards honest.

| Stage | Executed validation | Result |
| --- | --- | --- |
| RED | `npm test -- test/existing-bank-settlement.test.ts test/counterparty-groups.test.ts test/write-reconciliation.test.ts` | 18 failed, 123 passed, 3 files failed. Failures reproduced BP 3.0 fields/enumeration, unchanged advance account, overly broad readback, legacy hierarchy lookup, missing recovery inventory and group marker fallback. |
| GREEN | `npm test` | 598 passed in 36 files. |
| Quality | `npm run lint`, `npm run format:check`, `npm run typecheck`, `npm run build`, `npm run manifest` | All passed. Manifest rebuilt offline with synthetic connection. |
| Coverage attempt | `npm test -- --coverage` | Unavailable: `@vitest/coverage-v8` is not installed. No coverage percentage is claimed. |

Tests exercise actual BP 3.0 metadata and enumeration values, Belarus receipt/payment row schemas, both account resources, amount/tax/analytics protection, legitimate recalculated settlement fields, exact-ref group recovery, every operationId tool in the upstream recovery inventory, and the versionless Belarus register's explicit singleWriter update. Legacy creation tests verify zero hierarchy reads and retention of bounded register fallback. Mutation hierarchy cycle/deletion/depth/truncation tests remain strict.

Remaining limits: no live writes of this revised candidate; cross-currency conditional-unit contracts are explicitly refused because rate/settlement-amount conversion is not implemented. Group-membership changes and register writes with unknown outcomes remain manual/unverifiable and held; they are never advised to retry blindly. Post/Unpost only has a fresh version precheck, not a proven atomic CAS; versionless register updates still require sole-writer operation and have no server-side concurrency lock. Existing successful live evidence belongs to earlier candidates, not this revised head.
