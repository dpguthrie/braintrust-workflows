"""Shared helpers for the Braintrust workflow scripts.

Everything here is deliberately thin: one authenticated `requests.Session`
against the Braintrust REST API, a BTQL helper, an ingest->queryable poller,
and a metric emitter so each workflow doubles as a load-test probe.

Environment
-----------
  BRAINTRUST_API_KEY   (required)
  BRAINTRUST_API_URL   data-plane API base. Default https://api.braintrust.dev
                       For BYOC this is your own API endpoint.
  BRAINTRUST_APP_URL   app base, used only to build permalinks.
                       Default https://www.braintrust.dev
  BRAINTRUST_ORG_NAME  required if your key belongs to >1 org
  BT_PROJECT           project name the workflows write into.
                       Default "braintrust-workflows"

  # Optional mTLS (client certs in front of a self-hosted data plane)
  BRAINTRUST_CLIENT_CERT  path to client cert (PEM)
  BRAINTRUST_CLIENT_KEY   path to client key (PEM)
  BRAINTRUST_CA_BUNDLE    path to CA bundle, or "0" to disable verification

  # Metric output
  BT_METRICS_FILE      append one JSON object per timed step to this file
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import urllib.parse
import uuid
from typing import Any, Iterator

import requests

API_KEY = os.environ.get("BRAINTRUST_API_KEY")
API_URL = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
APP_URL = os.environ.get("BRAINTRUST_APP_URL", "https://www.braintrust.dev").rstrip("/")
ORG_NAME = os.environ.get("BRAINTRUST_ORG_NAME")
PROJECT_NAME = os.environ.get("BT_PROJECT", "braintrust-workflows")

_CLIENT_CERT = os.environ.get("BRAINTRUST_CLIENT_CERT")
_CLIENT_KEY = os.environ.get("BRAINTRUST_CLIENT_KEY")
_CA_BUNDLE = os.environ.get("BRAINTRUST_CA_BUNDLE")

# `query_source` shows up in Braintrust's own query logs. Tagging every request
# from these scripts makes it trivial to isolate workflow traffic from real
# traffic when reading the slow-query / lint dashboards during a load test.
QUERY_SOURCE = os.environ.get("BT_QUERY_SOURCE", "braintrust_workflows")


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def _tls_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if _CLIENT_CERT and _CLIENT_KEY:
        kw["cert"] = (_CLIENT_CERT, _CLIENT_KEY)
    elif _CLIENT_CERT:
        kw["cert"] = _CLIENT_CERT
    if _CA_BUNDLE == "0":
        kw["verify"] = False
    elif _CA_BUNDLE:
        kw["verify"] = _CA_BUNDLE
    return kw


class _MTLSAdapter(requests.adapters.HTTPAdapter):
    """Forces client certs / CA bundle onto every request.

    The Braintrust SDK opens its own `requests.Session` objects, so setting
    `session.cert` is not enough. `braintrust.set_http_adapter()` accepts an
    adapter, and `send()` is the one place both SDK and script traffic passes
    through. See `install_sdk_tls()`.
    """

    def send(self, request, **kwargs):  # type: ignore[override]
        kwargs.update(_tls_kwargs())
        return super().send(request, **kwargs)


_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        if not API_KEY:
            die("BRAINTRUST_API_KEY is not set")
        s = requests.Session()
        s.headers.update(
            {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            }
        )
        tls = _tls_kwargs()
        if "cert" in tls:
            s.cert = tls["cert"]
        if "verify" in tls:
            s.verify = tls["verify"]
        _session = s
    return _session


def install_sdk_tls() -> None:
    """Route the braintrust SDK's HTTP traffic through the same TLS config.

    No-op unless client certs / a custom CA are configured. Call this once,
    before any other braintrust SDK call.
    """
    if not (_CLIENT_CERT or _CA_BUNDLE):
        return
    import braintrust

    braintrust.set_http_adapter(_MTLSAdapter())


class BTError(RuntimeError):
    """A non-2xx response from the Braintrust API."""

    def __init__(self, method: str, url: str, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> {status}\n{body[:2000]}")


# Observed intermittently on otherwise-valid requests, both on /btql and on
# /v1/prompt: a 400 carrying a WASM-level runtime error rather than a real
# validation failure. The identical request succeeds on retry. Retry it a
# bounded number of times rather than failing a long load-test run on it.
_TRANSIENT = "memory access out of bounds"
RETRIES = int(os.environ.get("BT_RETRIES", "3"))


def _check(resp: requests.Response) -> Any:
    if not resp.ok:
        raise BTError(resp.request.method or "?", resp.url, resp.status_code, resp.text)
    if not resp.content:
        return None
    return resp.json()


def _request(method: str, path: str, **kwargs: Any) -> Any:
    url = path if path.startswith("http") else f"{API_URL}{path}"
    last: BTError | None = None
    for attempt in range(RETRIES):
        try:
            return _check(session().request(method, url, **kwargs))
        except BTError as e:
            if e.status != 400 or _TRANSIENT not in e.body:
                raise
            last = e
            metric("transient_400_retry", 0, path=path, attempt=attempt + 1)
            time.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    return _request("GET", path, params=params)


def api_post(path: str, body: dict[str, Any]) -> Any:
    return _request("POST", path, json=body)


def api_put(path: str, body: dict[str, Any]) -> Any:
    return _request("PUT", path, json=body)


def api_patch(path: str, body: dict[str, Any]) -> Any:
    return _request("PATCH", path, json=body)


# --------------------------------------------------------------------------
# BTQL
# --------------------------------------------------------------------------


def btql(query: str, **opts: Any) -> list[dict[str, Any]]:
    """Run one SQL/BTQL query and return its rows.

    Useful `opts`:
      version="<xact_id>"     read a dataset/experiment as of a transaction
      lint_mode="strict"      fail the query instead of warning on a bad plan
      brainstore_realtime=True  include not-yet-indexed rows (freshness probe)
      use_brainstore=True
    """
    body: dict[str, Any] = {"query": query, "fmt": "json", "query_source": QUERY_SOURCE}
    body.update(opts)
    return _request("POST", "/btql", json=body).get("data", [])


def btql_page(query: str, **opts: Any) -> tuple[list[dict[str, Any]], str | None]:
    body: dict[str, Any] = {"query": query, "fmt": "json", "query_source": QUERY_SOURCE}
    body.update(opts)
    resp = _request("POST", "/btql", json=body)
    return resp.get("data", []), resp.get("cursor")


def btql_all(query: str, max_rows: int = 100_000, **opts: Any) -> Iterator[dict[str, Any]]:
    """Paginate a query to completion.

    The query MUST carry a cursor-compatible sort -- `ORDER BY _pagination_key`
    or `ORDER BY _xact_id` -- or the cursor will not advance correctly.
    """
    cursor: str | None = None
    seen = 0
    while True:
        q = query if cursor is None else f"{query}\nOFFSET '{cursor}'"
        rows, cursor = btql_page(q, **opts)
        for r in rows:
            yield r
            seen += 1
            if seen >= max_rows:
                return
        if not cursor or not rows:
            return


def btql_scalar(query: str, **opts: Any) -> Any:
    rows = btql(query, **opts)
    if not rows:
        return None
    return next(iter(rows[0].values()))


# --------------------------------------------------------------------------
# objects
# --------------------------------------------------------------------------


def project_id(name: str = PROJECT_NAME) -> str:
    """Create-or-get a project. POST /v1/project is idempotent by name."""
    body: dict[str, Any] = {"name": name}
    if ORG_NAME:
        body["org_name"] = ORG_NAME
    return api_post("/v1/project", body)["id"]


def find_dataset(project: str, name: str) -> dict[str, Any] | None:
    rows = api_get("/v1/dataset", {"project_name": project, "dataset_name": name, "limit": 1})
    objs = rows.get("objects", []) if isinstance(rows, dict) else rows
    return objs[0] if objs else None


def snapshot_dataset(dataset_id: str, name: str, xact_id: str, description: str = "") -> dict[str, Any]:
    """Pin the current state of a dataset under a human-readable name.

    `xact_id` comes from `braintrust.init_dataset(...).version`, which is the
    max `_xact_id` across the dataset's rows. Reading the dataset back with
    `version=<xact_id>` reproduces exactly this state, edits and deletes
    included.
    """
    return api_post(
        "/v1/dataset_snapshot",
        {
            "dataset_id": dataset_id,
            "name": name,
            "xact_id": str(xact_id),
            "description": description or None,
        },
    )


def dataset_version(ds: Any) -> str:
    """Current `_xact_id` of a dataset -- the value you pin a read to.

    The SDK exposes `ds.version`, but reading it fetches every row to compute
    a max, which is not viable on a large dataset. Ask the query engine first
    and keep the SDK path as the fallback: `max(_xact_id)` has been observed to
    fail with an engine-level error immediately after a batch of writes.
    """
    for query in (
        f"SELECT max(_xact_id) AS v FROM dataset('{ds.id}')",
        f"SELECT _xact_id AS v FROM dataset('{ds.id}') ORDER BY _xact_id DESC LIMIT 1",
    ):
        try:
            v = btql_scalar(query)
            if v:
                return str(v)
        except BTError as e:
            warn(f"version query failed ({e.status}); falling back")
    return str(ds.version)


def list_snapshots(dataset_id: str) -> list[dict[str, Any]]:
    resp = api_get("/v1/dataset_snapshot", {"dataset_id": dataset_id, "limit": 100})
    return resp.get("objects", []) if isinstance(resp, dict) else resp


def log_permalink(pid: str, row_id: str, object_type: str = "project_logs") -> str:
    qs = urllib.parse.urlencode({"object_type": object_type, "object_id": pid, "id": row_id})
    org = ORG_NAME or "~"
    return f"{APP_URL}/app/{org}/object?{qs}"


def summary_scores(summary: Any) -> tuple[dict, dict]:
    """Read (scores, metrics) off an ExperimentSummary across SDK versions.

    Newer SDKs moved the maps under `summary.comparison`, which can be a
    `SummarySkipped` carrying a reason. The legacy `.scores` / `.metrics`
    properties still work but return `{}` on a skip, which reads exactly like
    "the eval produced no scores". Surface the reason instead.
    """
    comparison = getattr(summary, "comparison", None)
    if comparison is not None and getattr(comparison, "status", None) == "skipped":
        warn(f"experiment summary comparison skipped: {getattr(comparison, 'reason', 'unknown')}")
        return {}, {}
    return dict(summary.scores or {}), dict(summary.metrics or {})


def experiment_url(project: str, experiment_name: str) -> str:
    org = ORG_NAME or "~"
    return (
        f"{APP_URL}/app/{urllib.parse.quote(org)}/p/{urllib.parse.quote(project)}"
        f"/experiments/{urllib.parse.quote(experiment_name)}"
    )


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------


def wait_until_queryable(
    query: str,
    expect_at_least: int,
    timeout_s: float = 180.0,
    interval_s: float = 1.0,
    label: str = "ingest_to_queryable",
    **opts: Any,
) -> float:
    """Poll a COUNT query until it reaches `expect_at_least`.

    Returns seconds elapsed. This is the §5.1 "ingest -> queryable" metric:
    a span is not counted as ingested until a query can see it.
    """
    t0 = time.time()
    last = -1
    while True:
        got = btql_scalar(query, **opts) or 0
        if got != last:
            last = got
        if got >= expect_at_least:
            elapsed = time.time() - t0
            metric(label, elapsed * 1000, rows=got, expected=expect_at_least)
            return elapsed
        if time.time() - t0 > timeout_s:
            elapsed = time.time() - t0
            metric(label, elapsed * 1000, rows=got, expected=expect_at_least, timed_out=True)
            warn(f"{label}: timed out after {elapsed:.1f}s with {got}/{expect_at_least} rows queryable")
            return elapsed
        time.sleep(interval_s)


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

_RUN_ID = os.environ.get("BT_RUN_ID") or uuid.uuid4().hex[:12]
_METRICS_FILE = os.environ.get("BT_METRICS_FILE")


def metric(name: str, ms: float, **fields: Any) -> None:
    """Emit one structured timing record.

    Goes to stderr always, and to BT_METRICS_FILE as JSONL if set, so a load
    harness can collect p50/p95/p99 per workflow step without parsing prose.
    """
    rec = {"run_id": _RUN_ID, "ts": time.time(), "step": name, "ms": round(ms, 1), **fields}
    line = json.dumps(rec)
    print(f"    [metric] {line}", file=sys.stderr)
    if _METRICS_FILE:
        with open(_METRICS_FILE, "a") as f:
            f.write(line + "\n")


@contextlib.contextmanager
def timed(name: str, **fields: Any):
    t0 = time.time()
    try:
        yield
    finally:
        metric(name, (time.time() - t0) * 1000, **fields)


_step_n = 0


def step(text: str) -> None:
    global _step_n
    _step_n += 1
    print(f"\n\033[1m[{_step_n}] {text}\033[0m")


def info(text: str) -> None:
    print(f"    {text}")


def warn(text: str) -> None:
    print(f"    \033[33mWARN\033[0m {text}")


def die(text: str) -> None:
    print(f"\033[31mERROR\033[0m {text}", file=sys.stderr)
    raise SystemExit(1)


def banner(title: str) -> None:
    print(f"\n\033[1;36m{'=' * 72}\n{title}\n{'=' * 72}\033[0m")
    info(f"api_url={API_URL}  project={PROJECT_NAME}  run_id={_RUN_ID}")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

# A small synthetic support-QA corpus for a fictional product. Nothing here is
# real; it exists only so the workflows have deterministic inputs to move
# around. Swap it for your own domain when you adapt these scripts.
_TOPICS = [
    ("Which regions is the API available in?",
     "us-east, us-west, eu-central, and ap-southeast",
     "regions"),
    ("How do I rotate an API key?",
     "Settings > API keys > Rotate. The previous key stays valid for 24 hours",
     "auth"),
    ("Why did my request return 429?",
     "You exceeded the per-minute request limit for your plan tier",
     "ratelimits"),
    ("Which endpoints support pagination?",
     "Every list endpoint, through the cursor query parameter",
     "pagination"),
    ("How many seats does the team plan include?",
     "Depends on plan tier: 5, 25, or unlimited seats",
     "billing"),
    ("What is the maximum upload size?",
     "100 MB per file and 1 GB per request",
     "limits"),
    ("Can I call the SDK from a browser?",
     "No. The SDK requires a server runtime; call the public API from browsers",
     "sdk"),
    ("Why is my invoice higher this month?",
     "A plan change, or seats added partway through the billing cycle",
     "billing"),
]


def synthetic_case(i: int) -> dict[str, Any]:
    """One deterministic golden-dataset row.

    Deterministic on purpose: reruns at any size produce byte-identical rows,
    so ingest volume is a pure function of --rows.
    """
    question, answer, topic = _TOPICS[i % len(_TOPICS)]
    # Co-prime strides so surface and difficulty are independent. Using i % 3
    # for both would make them perfectly correlated, and every cross-dimension
    # filter (hard AND web) would return zero rows.
    surface = ["web", "cli", "api"][i % 3]
    difficulty = ["easy", "medium", "hard"][(i // 3) % 3]
    return {
        "input": {"question": f"{question} (case {i})", "locale": "en-US"},
        "expected": {"answer": answer},
        "metadata": {
            "case_index": i,
            "surface": surface,
            "difficulty": difficulty,
            "topic": topic,
        },
        "tags": ["golden", surface],
    }
