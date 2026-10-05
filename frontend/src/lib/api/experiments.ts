// Typed helpers for the workflow-experiment (A/B testing) endpoints. All
// requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// 401-bounce). Mirrors the pattern of `src/lib/api/recurring.ts`.
import { api } from '#lib/api.ts';
import type {
	Experiment,
	ExperimentCreate,
	ExperimentListResponse,
	ExperimentResults
} from '#lib/types/experiments.ts';

export function listExperiments(status?: string): Promise<ExperimentListResponse> {
	const qs = new URLSearchParams();
	if (status) qs.set('status', status);
	const q = qs.toString();
	return api.get<ExperimentListResponse>(`/api/experiments${q ? `?${q}` : ''}`);
}

export function createExperiment(body: ExperimentCreate): Promise<Experiment> {
	return api.post<Experiment>('/api/experiments', body);
}

export function startExperiment(id: string): Promise<Experiment> {
	return api.post<Experiment>(`/api/experiments/${id}/start`, {});
}

export function stopExperiment(id: string): Promise<Experiment> {
	return api.post<Experiment>(`/api/experiments/${id}/stop`, {});
}

export function concludeExperiment(id: string): Promise<Experiment> {
	return api.post<Experiment>(`/api/experiments/${id}/conclude`, {});
}

export function deleteExperiment(id: string): Promise<void> {
	return api.delete(`/api/experiments/${id}`);
}

export function getExperimentResults(id: string): Promise<ExperimentResults> {
	return api.get<ExperimentResults>(`/api/experiments/${id}/results`);
}
