import { portalApi } from '#lib/portalApi.ts';

export interface PortalPendingChange {
	id: string;
	change_type: string;
	status: string;
	created_at: string;
}

export interface PortalCompanyInfo {
	name: string;
	email: string | null;
	phone: string | null;
	address: string | null;
	tax_id_last4: string | null;
	has_bank_details: boolean;
	pending_change: PortalPendingChange | null;
}

export interface PortalChangeRequest {
	id: string;
	change_type: string;
	status: string;
	created_at: string;
}

export interface PortalTaxForm {
	on_file: boolean;
	form_type: string | null; // 'w9' | 'w8' | null
	received_date: string | null;
	suggested_form_type: string;
}

function createPortalCompany() {
	let info = $state<PortalCompanyInfo | null>(null);
	let changeRequests = $state<PortalChangeRequest[]>([]);
	let taxForm = $state<PortalTaxForm | null>(null);
	let loading = $state(false);
	let error = $state('');

	async function fetchCompany() {
		loading = true;
		error = '';
		try {
			info = await portalApi.get<PortalCompanyInfo>('/api/portal/company');
		} catch (err) {
			error = err instanceof Error ? err.message : 'Load failed';
		} finally {
			loading = false;
		}
	}

	async function updateContact(patch: {
		email?: string;
		phone?: string;
		address?: string;
	}) {
		info = await portalApi.patch<PortalCompanyInfo>('/api/portal/company', patch);
	}

	async function requestBankChange(bank_details: Record<string, unknown>) {
		await portalApi.post('/api/portal/company/bank-change', { bank_details });
		// Re-read so the "pending AP approval" banner shows immediately.
		await fetchCompany();
	}

	async function requestTaxIdChange(tax_id: string) {
		await portalApi.post('/api/portal/company/tax-id-change', { tax_id });
		await fetchCompany();
	}

	async function fetchChangeRequests() {
		changeRequests = await portalApi.get<PortalChangeRequest[]>(
			'/api/portal/company/change-requests'
		);
	}

	async function fetchTaxForm() {
		taxForm = await portalApi.get<PortalTaxForm>('/api/portal/company/tax-form');
	}

	async function uploadTaxForm(file: File, formType: string) {
		// `upload` sends multipart with the file under `file`; the form type
		// rides as an extra multipart field, matching the backend's Form(...).
		taxForm = await portalApi.upload<PortalTaxForm>('/api/portal/company/tax-form', file, {
			form_type: formType,
		});
	}

	function downloadTaxForm(): Promise<Blob> {
		return portalApi.download('/api/portal/company/tax-form/file');
	}

	return {
		get info() {
			return info;
		},
		get changeRequests() {
			return changeRequests;
		},
		get taxForm() {
			return taxForm;
		},
		get loading() {
			return loading;
		},
		get error() {
			return error;
		},
		fetchCompany,
		updateContact,
		requestBankChange,
		requestTaxIdChange,
		fetchChangeRequests,
		fetchTaxForm,
		uploadTaxForm,
		downloadTaxForm,
	};
}

export const portalCompany = createPortalCompany();
