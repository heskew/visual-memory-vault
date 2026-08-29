const assert = require("node:assert/strict");
const path = require("node:path");
const { pollJob, uploadPhoto } = require("../../frontend/static/vault-upload.js");

async function testUploadThenPoll() {
  const calls = [];
  const jobId = "11111111-1111-4111-8111-111111111111";
  const states = [
    { status: "pending", job_id: jobId, image_path: "/media/a.jpg" },
    {
      status: "succeeded",
      job_id: jobId,
      image_path: "/media/a.jpg",
      summary: "Saved dinner at Joe's Grill.",
      merchant: "Joe's Grill",
      amount: "58.40",
      currency: "USD",
      date: "2026-08-20",
    },
  ];
  let jobReads = 0;

  async function fetchImpl(url, opts) {
    calls.push({ url, method: (opts && opts.method) || "GET" });
    if (url === "/upload") {
      return {
        status: 202,
        text: async () =>
          JSON.stringify({
            status: "accepted",
            job_id: jobId,
            image_path: "/media/a.jpg",
          }),
      };
    }
    if (url === "/jobs/" + jobId) {
      const body = states[Math.min(jobReads, states.length - 1)];
      jobReads += 1;
      return { status: 200, text: async () => JSON.stringify(body) };
    }
    throw new Error("unexpected url " + url);
  }

  const file = { name: "receipt.jpg" };
  const accepted = await uploadPhoto(file, fetchImpl);
  assert.equal(accepted.status, "accepted");
  assert.equal(accepted.job_id, jobId);
  assert.ok(!("summary" in accepted));

  const job = await pollJob(accepted.job_id, fetchImpl, {
    interval: 1,
    timeout: 1000,
  });
  assert.equal(job.status, "succeeded");
  assert.equal(job.merchant, "Joe's Grill");
  assert.ok(calls.some((c) => c.url === "/upload" && c.method === "POST"));
  assert.ok(calls.some((c) => c.url.startsWith("/jobs/")));
  assert.ok(!calls.some((c) => String(c.url).includes("wait=1")));
}

testUploadThenPoll()
  .then(() => {
    console.log("ok", path.basename(__filename));
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
