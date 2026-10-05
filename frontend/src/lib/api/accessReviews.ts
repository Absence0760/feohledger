// Typed helpers for the periodic SOX access-review surface. Routes through the
// shared `api` client (Bearer + X-Tenant-Slug + 401-bounce) — never raw fetch.
// Backend: `backend/app/api/access_reviews.py` (admin/CFO gated).
import { api } from '#lib/api.ts';
import type {
	AccessReviewAcknowledgeResponse,
	AccessReviewResponse
} from '#lib/types/accessReview.ts';

/** Every active user holding an elevated role, with a computed dormancy
 *  verdict. The read itself is audited server-side (a sensitive read). */
export function getAccessReview(): Promise<AccessReviewResponse> {
	return api.get<AccessReviewResponse>('/api/access-reviews');
}

/** Record that a reviewer completed the access review for this period.
 *  Idempotent-friendly — re-acknowledging just re-stamps the timestamp. */
export function acknowledgeAccessReview(): Promise<AccessReviewAcknowledgeResponse> {
	return api.post<AccessReviewAcknowledgeResponse>('/api/access-reviews/acknowledge', {});
}
