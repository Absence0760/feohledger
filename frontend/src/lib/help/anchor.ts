// A section heading's anchor on its guide page: lowercase words joined by
// hyphens. Stable as long as the heading is, which is the trade every
// heading-derived anchor makes; content.test.ts fails on two headings in one
// guide that would share an anchor.

export function headingId(heading: string): string {
	return (
		heading
			.normalize('NFKD')
			.replace(/[̀-ͯ]/g, '')
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, '-')
			.replace(/^-|-$/g, '') || 'section'
	);
}
