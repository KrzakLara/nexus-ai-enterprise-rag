"""Generate docker/observability/grafana/dashboards/nexus-operations.json (Grafana provisioning).

Every panel is a PromQL query over the metrics defined in backend/app/observability/metrics.py;
nothing is precomputed or hardcoded. Re-run after changing metrics:
    python scripts/build_grafana_dashboard.py
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docker" / "observability" / "grafana" / "dashboards" / "nexus-operations.json"
DS = {"type": "prometheus", "uid": "prometheus"}


def q(expr: str, legend: str) -> dict:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": legend[:1].upper() or "A"}


def panel(pid: int, title: str, targets: list[dict], x: int, y: int, unit: str = "short", w: int = 12,
          h: int = 8, kind: str = "timeseries", description: str = "") -> dict:
    for i, t in enumerate(targets):
        t["refId"] = chr(65 + i)
    return {"id": pid, "type": kind, "title": title, "description": description, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}}}


def p_quantiles(metric: str, by: str = "", where: str = "") -> list[dict]:
    group = "le" + (f", {by}" if by else "")
    selector = f"{{{where}}}" if where else ""
    return [q(f"histogram_quantile({quantile}, sum by ({group}) (rate({metric}_bucket{selector}[5m])))",
              f"p{int(float(quantile) * 100)}" + (f" {{{{{by}}}}}" if by else ""))
            for quantile in ("0.5", "0.95", "0.99")]


panels = [
    panel(1, "API request rate by route", [q('sum by (route) (rate(nexus_http_requests_total{route!~"/api/health.*"}[1m]))', "{{route}}")],
          0, 0, "reqps"),
    panel(2, "HTTP error rate (4xx / 5xx share)", [
        q('sum(rate(nexus_http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(nexus_http_requests_total[5m])), 1e-9)', "5xx"),
        q('sum(rate(nexus_http_requests_total{status=~"4.."}[5m])) / clamp_min(sum(rate(nexus_http_requests_total[5m])), 1e-9)', "4xx")],
        12, 0, "percentunit"),
    panel(3, "API latency p50 / p95 / p99 (excl. health)",
          p_quantiles("nexus_http_request_duration_seconds", where='route!~"/api/health.*"'), 0, 8, "s"),
    panel(4, "Retrieval latency by mode (p95)", [q(
        "histogram_quantile(0.95, sum by (le, mode) (rate(nexus_retrieval_duration_seconds_bucket[5m])))", "{{mode}}")],
        12, 8, "s"),
    panel(5, "Queue depth by status", [q("sum by (status) (nexus_jobs_queue_depth)", "{{status}}")], 0, 16),
    panel(6, "Job throughput by outcome", [q("sum by (job_type, outcome) (rate(nexus_jobs_finished_total[5m])) * 60",
                                             "{{job_type}} {{outcome}}")], 12, 16, "short",
          description="Finished job attempts per minute"),
    panel(7, "Job duration p95 by type", [q(
        "histogram_quantile(0.95, sum by (le, job_type) (rate(nexus_job_duration_seconds_bucket[5m])))", "{{job_type}}")],
        0, 24, "s"),
    panel(8, "Queue wait p95 (run_after -> claim)", [q(
        "histogram_quantile(0.95, sum by (le, job_type) (rate(nexus_job_queue_wait_seconds_bucket[5m])))", "{{job_type}}")],
        12, 24, "s"),
    panel(9, "Processing stages p95 (download / parse incl. OCR / index / embed)", [q(
        "histogram_quantile(0.95, sum by (le, stage) (rate(nexus_processing_stage_duration_seconds_bucket[5m])))", "{{stage}}")],
        0, 32, "s"),
    panel(10, "OCR: pages per minute and parse time of OCR documents (p95)", [
        q("rate(nexus_ocr_pages_total[5m]) * 60", "OCR pages / min"),
        q("histogram_quantile(0.95, sum by (le) (rate(nexus_ocr_document_parse_seconds_bucket[5m])))", "parse p95 (s)")],
        12, 32),
    panel(11, "Worker failures and retries", [
        q("sum by (error_code) (increase(nexus_job_failures_total[15m]))", "failed {{error_code}}"),
        q("sum by (error_code) (increase(nexus_job_retries_total[15m]))", "retry {{error_code}}")], 0, 40),
    panel(12, "Workers busy / last heartbeat age", [
        q("sum(nexus_worker_busy)", "busy workers"),
        q("time() - max(nexus_worker_last_heartbeat_timestamp_seconds > 0)", "heartbeat age (s)")], 12, 40),
    panel(13, "RAG answer latency p95 by answer mode", [q(
        "histogram_quantile(0.95, sum by (le, answer_mode) (rate(nexus_rag_answer_duration_seconds_bucket[5m])))",
        "{{answer_mode}}")], 0, 48, "s"),
    panel(14, "Authentication failures by reason", [q("sum by (method, reason) (rate(nexus_auth_failures_total[5m])) * 60",
                                                      "{{method}} {{reason}} (per min)")], 12, 48),
    panel(15, "Object storage operations", [q("sum by (operation, outcome) (rate(nexus_object_storage_operations_total[5m]))",
                                              "{{operation}} {{outcome}}")], 0, 56, "ops"),
    panel(16, "Database pool connections", [q("sum by (engine, state) (nexus_db_pool_connections)", "{{engine}} {{state}}")],
          12, 56),
    panel(17, "Document ingestion duration p95 by format", [q(
        "histogram_quantile(0.95, sum by (le, format) (rate(nexus_document_ingestion_duration_seconds_bucket[15m])))",
        "{{format}}")], 0, 64, "s"),
    panel(18, "Extractions and review actions", [
        q("sum by (provider, outcome) (increase(nexus_extractions_total[15m]))", "extraction {{provider}} {{outcome}}"),
        q("sum by (action) (increase(nexus_review_actions_total[15m]))", "review {{action}}")], 12, 64),
]

dashboard = {
    "uid": "nexus-operations", "title": "NEXUS AI — Operations", "tags": ["nexus", "phase4"],
    "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": False, "refresh": "10s",
    "time": {"from": "now-30m", "to": "now"}, "panels": panels,
    "description": "API latency and errors, background jobs and queue, OCR / extraction / embedding, retrieval, "
                   "authentication, storage and DB pool. Generated by scripts/build_grafana_dashboard.py.",
}

if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(panels)} panels)")
