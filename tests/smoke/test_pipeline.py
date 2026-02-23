"""E2E smoke test — validates the full Langfuse telemetry pipeline.

Requires:
    - Docker Compose stack running (agenticore, redis, postgres, otel-collector)
    - SMOKE_AGENTICORE_URL env var (default: http://localhost:8200)
    - SMOKE_DATASET_REPO env var (the test repo to clone)
    - LANGFUSE_* env vars (for trace verification)
    - CF_ACCESS_* env vars (if Langfuse is behind Cloudflare Access)

Run:
    pytest tests/smoke/test_pipeline.py -v --timeout=300
"""

import os
import time

import httpx
import pytest

AGENTICORE_URL = os.getenv("SMOKE_AGENTICORE_URL", "http://localhost:8200")
DATASET_REPO = os.getenv(
    "SMOKE_DATASET_REPO",
    "https://github.com/The-Cloud-Clock-Work/bridge-smoke-dataset",
)
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "")
CF_ACCESS_CLIENT_ID = os.getenv("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.getenv("CF_ACCESS_CLIENT_SECRET", "")


def _cf_headers() -> dict:
    """Build Cloudflare Access headers if credentials are available."""
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        return {
            "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
            "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
        }
    return {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def completed_job():
    """Submit a job and block until completion. Shared across all tests."""
    resp = httpx.post(
        f"{AGENTICORE_URL}/jobs",
        json={
            "task": "List all files in the repository root directory",
            "repo_url": DATASET_REPO,
            "profile": "code",
            "wait": True,
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    assert data.get("success"), f"Job submission failed: {data}"
    return data["job"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSmokePipeline:
    """End-to-end smoke tests for the agenticore + OTEL + Langfuse pipeline."""

    def test_health_check(self):
        """GET /health returns 200 with expected payload."""
        resp = httpx.get(f"{AGENTICORE_URL}/health", timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "agenticore"

    def test_profiles_include_defaults(self):
        """GET /profiles returns at least the built-in 'code' profile."""
        resp = httpx.get(f"{AGENTICORE_URL}/profiles", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success")
        names = [p["name"] for p in data["profiles"]]
        assert "code" in names, f"Expected 'code' in {names}"

    def test_submit_and_complete_job(self, completed_job):
        """Job ran to completion (succeeded or failed — not stuck in queued)."""
        assert completed_job["status"] in (
            "succeeded",
            "failed",
        ), f"Unexpected status: {completed_job['status']}"

    def test_job_has_session_id(self, completed_job):
        """Claude session_id was extracted from the subprocess output."""
        if completed_job["status"] != "succeeded":
            pytest.skip("Job did not succeed — session_id may be absent")
        assert completed_job.get("session_id"), "session_id should be set on succeeded jobs"

    def test_job_retrievable(self, completed_job):
        """GET /jobs/{id} returns the job with output or error captured."""
        job_id = completed_job["id"]
        resp = httpx.get(f"{AGENTICORE_URL}/jobs/{job_id}", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success")
        job = data["job"]
        has_content = job.get("output") or job.get("error")
        assert has_content, "Job should have output (if succeeded) or error (if failed)"

    @pytest.mark.skipif(
        not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY or not LANGFUSE_HOST,
        reason="LANGFUSE credentials not set — skipping trace verification",
    )
    def test_langfuse_trace_exists(self, completed_job):
        """Langfuse received a trace for this job via the OTEL collector."""
        if completed_job["status"] != "succeeded":
            pytest.skip("Job did not succeed — trace may not exist")

        job_id = completed_job["id"]

        # Give the OTEL collector time to flush batches to Langfuse
        time.sleep(10)

        # Search for the trace via list endpoint (direct GET is broken on Langfuse v2)
        headers = {**_cf_headers()}
        resp = httpx.get(
            f"{LANGFUSE_HOST}/api/public/traces",
            params={"limit": 10, "name": "agenticore-job"},
            auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
            headers=headers,
            timeout=15,
        )
        assert resp.status_code == 200, f"Langfuse API failed: {resp.status_code}"

        data = resp.json()
        traces = data.get("data", [])
        trace_ids = [t["id"] for t in traces]
        assert job_id in trace_ids, (
            f"Trace for job {job_id} not found in Langfuse. Found {len(traces)} agenticore-job traces: {trace_ids}"
        )
