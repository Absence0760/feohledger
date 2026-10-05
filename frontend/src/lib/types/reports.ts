/**
 * Type contracts for the Custom (ad-hoc) Report Builder — mirrors the
 * authoritative API contract consumed by `$lib/api/reports.ts` and the
 * `/reports` route. Endpoints live under `/api/reports` (`backend/app/api/reports.py`).
 *
 * Security model (enforced server-side): the client only ever sends **catalog
 * keys** — never raw SQL, column, or table names. The backend maps keys →
 * server-defined columns via a hardcoded whitelist; any key/op/agg not in the
 * catalog is rejected with 422. So the builder UI must be driven entirely by
 * `GET /api/reports/catalog`, never a hardcoded field list.
 *
 * Money values arrive as exact decimal **strings** (never float) and render
 * through `<Money>` / `formatMoney` — never re-computed client-side. Dates are
 * ISO-8601 strings.
 */

/** Aggregation functions a measure may be rolled up with. */
export type AggFn = 'sum' | 'avg' | 'count' | 'min' | 'max';

/** Comparison operators a filter may use (server validates op ↔ field type). */
export type FilterOp =
	| 'eq'
	| 'ne'
	| 'gt'
	| 'gte'
	| 'lt'
	| 'lte'
	| 'in'
	| 'contains'
	| 'between';

/** Bucketing grain for a date dimension (default `month`). */
export type DateGrain = 'day' | 'month' | 'quarter' | 'year';

/** Sort direction. */
export type SortDir = 'asc' | 'desc';

/** Field type as reported by the catalog. Dimensions are string/date/enum;
 *  measures are money/number. Filters can carry any of these. */
export type FieldType = 'string' | 'date' | 'enum' | 'money' | 'number';

// ---------------------------------------------------------------------------
// Catalog (GET /api/reports/catalog) — drives the whole builder UI.
// ---------------------------------------------------------------------------

export interface CatalogDimension {
	key: string;
	label: string;
	type: 'string' | 'date' | 'enum';
}

export interface CatalogMeasure {
	key: string;
	label: string;
	/** Aggregations allowed on this measure (e.g. `["sum","avg"]`). */
	aggs: AggFn[];
	type: 'money' | 'number';
}

export interface CatalogFilter {
	key: string;
	label: string;
	type: FieldType;
	/** Operators the server will accept for this field. */
	ops: FilterOp[];
	/** Present for enum fields — the allowed values to offer in the UI. */
	enumValues?: string[];
}

export interface CatalogSource {
	key: string;
	label: string;
	dimensions: CatalogDimension[];
	measures: CatalogMeasure[];
	filters: CatalogFilter[];
}

export interface ReportCatalog {
	sources: CatalogSource[];
}

// ---------------------------------------------------------------------------
// Spec (the query the client builds and sends).
// ---------------------------------------------------------------------------

export interface SpecDimension {
	/** A `CatalogDimension.key`. */
	key: string;
	/** Only meaningful for date dimensions; null / omitted for the rest. */
	grain?: DateGrain | null;
}

export interface SpecMeasure {
	/** A `CatalogMeasure.key`. */
	key: string;
	agg: AggFn;
}

/** A filter's value shape depends on its operator:
 *  - `in`      → an array of values,
 *  - `between` → a two-element `[from, to]` tuple,
 *  - everything else → a single scalar. */
export type FilterValue = string | number | boolean | Array<string | number> | null;

export interface SpecFilter {
	/** A `CatalogFilter.key`. */
	key: string;
	op: FilterOp;
	value: FilterValue;
}

export interface SpecSort {
	/** A dimension key OR `<measure_key>_<agg>` (e.g. `amount_sum`). */
	key: string;
	dir: SortDir;
}

export interface ReportSpec {
	data_source: string;
	dimensions: SpecDimension[];
	measures: SpecMeasure[];
	filters: SpecFilter[];
	sort: SpecSort[];
	/** Optional hard row cap independent of pagination. */
	limit?: number | null;
}

// ---------------------------------------------------------------------------
// Result (POST /api/reports/run, POST /api/reports/{id}/run).
// ---------------------------------------------------------------------------

export interface ResultColumn {
	/** Column key present on every row object (e.g. `vendor_name`, `amount_sum`). */
	key: string;
	label: string;
	kind: 'dimension' | 'measure';
	/** Rendering hint — `money` (a measure) renders via `<Money>`. A dimension
	 *  column carries its dimension type (`string` / `date` / `enum`). */
	type?: FieldType | null;
	/** Money measures only: the key of the dimension column whose value on the
	 *  SAME row is the currency that row's figure is denominated in. The server
	 *  always groups a money aggregate by it (it never sums across currencies),
	 *  so each cell is labelled with its own row's code — never the org's. */
	currency_key?: string | null;
}

/** A result row: a flat map of column-key → value. Money cells are exact
 *  decimal strings; counts / numbers are numbers; dimensions are strings. */
export type ReportRow = Record<string, string | number | null>;

export interface ReportResult {
	columns: ResultColumn[];
	rows: ReportRow[];
	/** Total group count across all pages (drives pagination). */
	total_rows: number;
	page: number;
	page_size: number;
}

// ---------------------------------------------------------------------------
// Saved definitions.
// ---------------------------------------------------------------------------

export interface ReportDefinition extends ReportSpec {
	id: string;
	name: string;
	description: string | null;
	created_by_user_id: string | null;
	created_at: string;
	updated_at: string;
}

export interface ReportListResponse {
	reports: ReportDefinition[];
}

/** Body for `POST /api/reports` — a spec plus a name / optional description. */
export type CreateReportBody = ReportSpec & {
	name: string;
	description?: string | null;
};

/** Body for `PATCH /api/reports/{id}` — any subset of the saved definition. */
export type UpdateReportBody = Partial<CreateReportBody>;

/** Human labels for the aggregation functions. */
export const AGG_LABELS: Record<AggFn, string> = {
	sum: 'Sum',
	avg: 'Average',
	count: 'Count',
	min: 'Min',
	max: 'Max'
};

/** Human labels for the filter operators. */
export const OP_LABELS: Record<FilterOp, string> = {
	eq: 'is',
	ne: 'is not',
	gt: 'greater than',
	gte: 'at least',
	lt: 'less than',
	lte: 'at most',
	in: 'is any of',
	contains: 'contains',
	between: 'between'
};

/** Human labels for the date grains. */
export const GRAIN_LABELS: Record<DateGrain, string> = {
	day: 'Day',
	month: 'Month',
	quarter: 'Quarter',
	year: 'Year'
};

/** The result-column key a measure produces once aggregated (`<key>_<agg>`).
 *  Matches the backend's column-key convention so `sort` keys line up. */
export function measureColumnKey(m: SpecMeasure): string {
	return `${m.key}_${m.agg}`;
}

/** The currency a money cell is denominated in: the value, on the SAME row, of
 *  the dimension column its measure names in `currency_key`. Never the org's
 *  currency — a GBP-reporting tenant holds USD invoices. `null` when the column
 *  names none or the row carries no code, which `formatMoney` renders bare
 *  rather than guessing (decisions §160, §196, §228). */
export function cellCurrency(col: ResultColumn, row: ReportRow): string | null {
	if (!col.currency_key) return null;
	const code = row[col.currency_key];
	return typeof code === 'string' && code.trim() ? code : null;
}
