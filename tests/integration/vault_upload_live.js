/* Live-client contract for vault-upload.js against a running proxy. */
const fs = require("node:fs");
const { pollJob, uploadPhoto } = require("../../frontend/static/vault-upload.js");

const base = process.env.PROXY_BASE;
const apiKey = process.env.PROXY_API_KEY || "";

if (!base) {
  console.error("PROXY_BASE is required");
  process.exit(1);
}

async function fetchImpl(url, opts) {
  const options = opts || {};
  if (String(url).includes("wait=1")) {
    throw new Error("client must not call wait=1");
  }
  const headers = Object.assign({}, options.headers || {}, {
    "X-Api-Key": apiKey,
  });
  return fetch(base + url, Object.assign({}, options, { headers }));
}

async function main() {
  const cmd = process.argv[2];
  if (cmd === "upload") {
    const bytes = fs.readFileSync(process.env.UPLOAD_FILE);
    const file = new File([bytes], "receipt.jpg", { type: "image/jpeg" });
    const accepted = await uploadPhoto(file, fetchImpl);
    if (accepted.status !== "accepted") {
      throw new Error("expected accepted, got " + JSON.stringify(accepted));
    }
    if (!accepted.job_id || accepted.summary || accepted.reply) {
      throw new Error("202 body must be job_id only: " + JSON.stringify(accepted));
    }
    console.log(JSON.stringify(accepted));
    return;
  }
  if (cmd === "poll") {
    const job = await pollJob(process.argv[3], fetchImpl, {
      interval: 50,
      timeout: 4000,
    });
    console.log(JSON.stringify(job));
    return;
  }
  if (cmd === "poll-timeout") {
    try {
      await pollJob(process.argv[3], fetchImpl, {
        interval: 40,
        timeout: 180,
      });
      throw new Error("pending job must not look terminal to pollJob");
    } catch (err) {
      if (!String(err.message).includes("Timed out waiting for job")) {
        throw err;
      }
    }
    console.log(JSON.stringify({ timed_out: true }));
    return;
  }
  throw new Error("usage: vault_upload_live.js upload|poll|poll-timeout [job_id]");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
