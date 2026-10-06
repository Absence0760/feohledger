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
	/** Present when the row was refused for a reason the client localizes: the
	 *  refusal's structured body sits beside `row` (today only the GL-chart
	 *  refusal — `code: 'gl_codes_outside_chart'` plus its params; see
	 *  `api/glChartRefusal.ts`). Render through `localizeApiDetail(error, m) ??
	 *  error.message`. */
	code?: string;
}

export interface ImportResult {
	imported: number;
	skipped: number;
	errors: ImportRowError[];
}
