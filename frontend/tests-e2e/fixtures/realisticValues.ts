import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The production-shaped value set (#575 § 2), read from the ONE file that
 * spells it: `backend/scripts/fixtures/realistic_rows.json`. The full and lean
 * seed read the same file (`backend/scripts/seed_realistic.py`), so the layout
 * specs (`realisticRows.ts`) and the day-one journey (`day-one/fixtures.ts`)
 * measure exactly the strings a developer sees in the seeded app.
 *
 * Read with `fs`, not a JSON `import`: Playwright's loader runs these files as
 * ESM, which refuses a JSON import without a `with { type: 'json' }` attribute,
 * and its transform does not carry the attribute through. The type still comes
 * from the file itself, so a renamed key fails `pnpm check:e2e`.
 */
type RealisticValues = typeof import('../../../backend/scripts/fixtures/realistic_rows.json');

const HERE = dirname(fileURLToPath(import.meta.url));

export const SHARED: RealisticValues = JSON.parse(
	readFileSync(resolve(HERE, '../../../backend/scripts/fixtures/realistic_rows.json'), 'utf8')
);
