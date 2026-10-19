// CloudFront Function (viewer-request) for the SPA distribution (infra/frontend.tf).
//
// adapter-static builds ONE document, index.html (`fallback: "index.html"`,
// frontend/vite.config.ts), and every route renders client-side from it. So a
// request is either for a file the build emitted, which passes through to S3,
// or for a route, which is rewritten to /index.html. Caddy decided the same
// thing with `try_files {path} /index.html` — but it could check whether the
// file existed, and a CloudFront Function cannot reach S3.
//
// The obvious stand-in, "has a file extension → it is a file", is wrong: a route
// segment can contain a dot (an invoice number, a vendor name, an email in a
// path), and that page would 404 on reload. So the files are named instead:
// everything under the two asset directories, plus each root-level file from
// frontend/static/. frontend/src/lib/deploy/spaRouter.test.ts runs this exact
// code against the real build output and fails if a built file would be
// rewritten, or if static/ gains a root entry missing from the lists below.
//
// A request for a missing file under a listed prefix stays a missing file: S3
// answers 404 (the bucket policy grants CloudFront s3:ListBucket for exactly
// that, without which S3 says 403), never index.html with a 200.
//
// cloudfront-js-2.0 runtime. No exports: CloudFront calls `handler` by name.

var FILE_PREFIXES = ['/_app/', '/marketing/'];

var ROOT_FILES = [
	'/apple-touch-icon.png',
	'/email-mark.png',
	'/favicon.ico',
	'/favicon.svg',
	'/icon-192.png',
	'/icon-512.png',
	'/icon-maskable-512.png',
	'/index.html',
	'/logo-mark.svg',
	'/manifest.webmanifest',
	'/og-image.png'
];

function handler(event) {
	var request = event.request;
	var uri = request.uri;

	for (var i = 0; i < FILE_PREFIXES.length; i++) {
		if (uri.indexOf(FILE_PREFIXES[i]) === 0) {
			return request;
		}
	}
	if (ROOT_FILES.indexOf(uri) !== -1) {
		return request;
	}

	// The query string travels separately (request.querystring), so a route's
	// ?filter=… state survives the rewrite untouched.
	request.uri = '/index.html';
	return request;
}
