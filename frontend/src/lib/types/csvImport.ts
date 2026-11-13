// Shared shape of the Day-0 CSV importers — `POST /api/vendors/import-csv`
// and `POST /api/invoices/import-csv`. Both are skip-and-report: a bad row
// never aborts the batch, it's just counted and explained. Mirrors the
// backend's `services/csv_import.ImportResult.to_dict()`. See
// backend/docs/csv-import.md.

export interface ImportRowError {
	/** The row's literal line number in the uploaded file — header is line 1, so the first data row is 2. */
	row: number;
	/** The server's English sentence — the fallback rendering. */
	message: string;
	/** The refusal's code. Every vendor and invoice import row error carries
	 *  one: the GL-chart refusal's structured body (`gl_codes_outside_chart`,
	 *  see `api/glChartRefusal.ts`) or `{code, params}` (the `csv_*` codes in
	 *  `api/codedRefusals.ts`, decisions §372). Render through
	 *  `localizeApiDetail(error, m) ?? error.message`. */
	code?: string;
	params?: Record<string, unknown>;
}

export interface ImportResult {
	imported: number;
	skipped: number;
	errors: ImportRowError[];
}
