// CloudWatch Synthetics canary (infra/canary.tf): is the certificate Caddy
// serves on api.<domain> valid, and does it have at least MIN_DAYS left?
//
// Route 53's health checks (infra/uptime.tf) complete the TLS handshake without
// validating the certificate, and ACM's DaysToExpiry only sees ACM's own
// certificate. The API's certificate is Let's Encrypt, obtained and renewed by
// Caddy on the VM, so nothing else in AWS can tell an expired or wrong one from
// a good one. This asks the way a browser does:
//
//   - Node's TLS stack validates the chain and the host name
//     (rejectUnauthorized is on, and stated so nobody turns it off), so an
//     expired, self-signed or wrong-host certificate fails the handshake;
//   - a valid certificate with fewer than MIN_DAYS left fails the run too. Caddy
//     renews 30 days ahead and retries, so under 14 means renewal has been
//     failing for over two weeks;
//   - the body must be the liveness probe's (backend/app/api/health.py), so a
//     valid certificate on the wrong server does not pass.
//
// Throwing fails the run; returning passes it. Plain node:https, no browser:
// this runtime ships Chromium, but nothing here launches it.

const https = require("node:https");

const HOST = process.env.CANARY_HOST;
const PATH = process.env.CANARY_PATH || "/api/health";
const MIN_DAYS = Number(process.env.CANARY_MIN_DAYS || "14");
const EXPECT = process.env.CANARY_EXPECT || '"status":"ok"';
const TIMEOUT_MS = 10000;
const DAY_MS = 24 * 60 * 60 * 1000;

function fetchWithCertificate() {
  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        host: HOST,
        servername: HOST,
        port: 443,
        path: PATH,
        method: "GET",
        rejectUnauthorized: true,
        // A fresh connection every run: a pooled socket would skip the
        // handshake, and the certificate with it.
        agent: false,
        headers: { "user-agent": "feohledger-certificate-canary" },
        timeout: TIMEOUT_MS,
      },
      (res) => {
        const cert = res.socket.getPeerCertificate();
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          if (body.length < 4096) body += chunk;
        });
        res.on("end", () => resolve({ status: res.statusCode, body, cert }));
      },
    );
    req.on("timeout", () => req.destroy(new Error(`no response from ${HOST} in ${TIMEOUT_MS} ms`)));
    req.on("error", reject);
    req.end();
  });
}

const checkCertificate = async function () {
  if (!HOST) throw new Error("CANARY_HOST is not set");

  const { status, body, cert } = await fetchWithCertificate();
  if (!cert || !cert.valid_to) {
    throw new Error(`https://${HOST} presented no certificate`);
  }

  const expires = new Date(cert.valid_to);
  const daysLeft = Math.floor((expires.getTime() - Date.now()) / DAY_MS);
  console.log(
    `https://${HOST}: certificate for ${cert.subject && cert.subject.CN} from ` +
      `${cert.issuer && cert.issuer.O}, expires ${expires.toISOString()} (${daysLeft} days)`,
  );

  if (daysLeft < MIN_DAYS) {
    throw new Error(
      `certificate on ${HOST} expires in ${daysLeft} days (under ${MIN_DAYS}): ` +
        "Caddy's renewal is failing — check `docker compose logs caddy` on the VM",
    );
  }
  if (status !== 200 || !body.includes(EXPECT)) {
    throw new Error(`https://${HOST}${PATH} answered ${status} without ${EXPECT}`);
  }
  return `certificate on ${HOST} valid for ${daysLeft} more days`;
};

exports.handler = async () => {
  return await checkCertificate();
};
