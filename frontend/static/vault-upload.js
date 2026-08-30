/* Capture upload + job poll for Visual Memory Vault (browser and Node tests). */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.VaultUpload = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  async function readJson(res) {
    const raw = await res.text();
    try {
      return JSON.parse(raw);
    } catch {
      throw new Error(
        "Server returned non-JSON (HTTP " + res.status + "). " + raw.slice(0, 300)
      );
    }
  }

  async function pollJob(jobId, fetchImpl, opts) {
    const options = opts || {};
    const interval = options.interval == null ? 400 : options.interval;
    const timeout = options.timeout == null ? 120000 : options.timeout;
    const deadline = Date.now() + timeout;
    if (!jobId) throw new Error("Missing job_id");
    while (Date.now() < deadline) {
      const res = await fetchImpl("/jobs/" + encodeURIComponent(jobId));
      const data = await readJson(res);
      if (res.status === 404) throw new Error("Job not found");
      if (res.status === 401) throw new Error("Unauthorized");
      if (data.status === "succeeded" || data.status === "failed") return data;
      await sleep(interval);
    }
    throw new Error("Timed out waiting for job");
  }

  async function uploadPhoto(file, fetchImpl) {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("subject", file.name);
    const res = await fetchImpl("/upload", { method: "POST", body: formData });
    const data = await readJson(res);
    if (res.status !== 202) {
      throw new Error(data.detail || "Upload was not accepted");
    }
    return data;
  }

  return { pollJob, uploadPhoto, readJson };
});
