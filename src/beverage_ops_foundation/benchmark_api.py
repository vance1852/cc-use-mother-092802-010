"""跨工厂质量对标平台的 HTTP/JSON 路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError


def route_benchmark(service, method: str, path: str, body: dict[str, Any],
                    headers: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
    """处理对标平台请求；不匹配时返回 None 交由基础路由处理。"""

    actor_id = headers.get("X-Actor-Id", "")
    parsed = urlparse(path)
    query = parse_qs(parsed.query)

    def one(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    def write(fn, created: bool = False) -> tuple[int, dict[str, Any]]:
        response = fn()
        if response.get("replayed"):
            return 200, response
        return (201 if created else 200), response

    try:
        if method == "POST" and parsed.path == "/metrics":
            return write(lambda: service.define_metric(actor_id=actor_id, **body), created=True)
        if method == "GET" and parsed.path == "/metrics":
            return 200, service.get_metric(one("metric_id", ""))
        if method == "POST" and parsed.path == "/metrics/retire":
            return write(lambda: service.retire_metric(actor_id=actor_id, **body))

        if method == "POST" and parsed.path == "/cycles":
            return write(lambda: service.create_cycle(actor_id=actor_id, **body), created=True)
        if method == "POST" and parsed.path == "/cycles/metrics":
            return write(lambda: service.add_cycle_metric(actor_id=actor_id, **body), created=True)
        if method == "POST" and parsed.path == "/cycles/sites":
            return write(lambda: service.add_cycle_site(actor_id=actor_id, **body), created=True)
        if method == "POST" and parsed.path == "/cycles/adjustments":
            return write(lambda: service.set_adjustment(actor_id=actor_id, **body), created=True)
        if method == "POST" and parsed.path == "/cycles/freeze":
            return write(lambda: service.freeze_cycle(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/cycles/ranking":
            return 200, service.frozen_ranking(one("cycle_id", ""))
        if method == "POST" and parsed.path == "/cycles/publish":
            return write(lambda: service.publish_cycle(actor_id=actor_id, **body), created=True)

        if method == "POST" and parsed.path == "/evidence":
            return write(lambda: service.submit_evidence(actor_id=actor_id, **body), created=True)
        if method == "GET" and parsed.path == "/evidence":
            return 200, {"items": service.list_evidence(
                one("cycle_id", ""), one("site_id", ""), one("metric_id"))}

        if method == "POST" and parsed.path == "/exclusions/request":
            return write(lambda: service.request_exclusion(actor_id=actor_id, **body), created=True)
        if method == "POST" and parsed.path == "/exclusions/review":
            return write(lambda: service.review_exclusion(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/exclusions":
            return 200, service.get_exclusion(one("exclusion_id", ""))

        if method == "POST" and parsed.path == "/disputes":
            return write(lambda: service.raise_dispute(actor_id=actor_id, **body), created=True)
        if method == "POST" and parsed.path == "/disputes/resolve":
            return write(lambda: service.resolve_dispute(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/disputes/withdraw":
            return write(lambda: service.withdraw_dispute(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/disputes":
            return 200, {"items": service.list_disputes(
                one("cycle_id", ""), one("status"))}

        if method == "GET" and parsed.path == "/publications":
            version_raw = one("version")
            version = int(version_raw) if version_raw else None
            return 200, service.get_publication(one("cycle_id", ""), version)

        if method == "POST" and parsed.path == "/findings":
            return write(lambda: service.create_finding(actor_id=actor_id, **body), created=True)
        if method == "GET" and parsed.path == "/findings":
            return 200, {"items": service.list_findings(
                one("cycle_id", ""), one("site_id"))}

        if method == "POST" and parsed.path == "/rectification-plans":
            return write(lambda: service.create_rectification_plan(actor_id=actor_id, **body), created=True)
        if method == "GET" and parsed.path == "/rectification-plans":
            return 200, service.get_plan(one("plan_id", ""))
        if method == "POST" and parsed.path == "/verifications":
            return write(lambda: service.submit_verification(actor_id=actor_id, **body), created=True)
        if method == "POST" and parsed.path == "/verifications/review":
            return write(lambda: service.review_verification(actor_id=actor_id, **body))

        if method == "POST" and parsed.path == "/batch-releases":
            return write(lambda: service.decide_batch_release(actor_id=actor_id, **body), created=True)
        if method == "GET" and parsed.path == "/batch-releases":
            return 200, {"items": service.list_batch_releases(
                one("site_id", ""), one("batch_no"))}

        if method == "GET" and parsed.path == "/trace-score":
            return 200, service.trace_score(
                one("cycle_id", ""), one("site_id", ""), one("metric_id", ""))
        return None
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
