"""跨工厂质量对标平台的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from beverage_ops_foundation.api import route as foundation_route
from beverage_ops_foundation.errors import DomainError, ValidationError
from beverage_ops_foundation.models import WriteReceipt

from .service import QualityService
from .storage import QualityDatabase


def _receipt_response(receipt: WriteReceipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def route(service: QualityService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """分派质量对标接口，其余路径回退到基础服务路由。"""

    parsed = urlparse(path)
    if not parsed.path.startswith("/quality"):
        return foundation_route(service, method, path, body, headers)
    headers = headers or {}
    body = body or {}
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)

    def param(name: str) -> str:
        value = query.get(name, [""])[0]
        if not value:
            raise ValidationError(f"{name} 不能为空")
        return value

    try:
        if method == "POST" and parsed.path == "/quality/metric-versions":
            return _receipt_response(service.define_metric_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/metric-versions/activate":
            return _receipt_response(service.activate_metric_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/cycles":
            return _receipt_response(service.create_cycle(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/cycles/freeze":
            return _receipt_response(service.freeze_cycle(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/cycles/publish":
            return _receipt_response(service.publish_cycle(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/submissions":
            return _receipt_response(service.submit_evidence(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/exclusions":
            return _receipt_response(service.request_exclusion(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/exclusions/review":
            return _receipt_response(service.review_exclusion(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/disputes":
            return _receipt_response(service.raise_dispute(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/disputes/resolve":
            return _receipt_response(service.resolve_dispute(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/plans":
            return _receipt_response(service.create_corrective_plan(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/plans/evidence":
            return _receipt_response(service.submit_plan_evidence(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/plans/verify":
            return _receipt_response(service.verify_corrective_plan(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/grants":
            return _receipt_response(service.grant_permission(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/quality/release-decisions":
            return _receipt_response(service.record_release_decision(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/quality/metric-versions":
            metric_id = query.get("metric_id", [None])[0]
            return 200, {"items": service.list_metric_versions(metric_id)}
        if method == "GET" and parsed.path == "/quality/cycles":
            return 200, service.get_cycle(param("cycle_id"))
        if method == "GET" and parsed.path == "/quality/rankings":
            metric_id = query.get("metric_id", [None])[0]
            return 200, service.get_ranking(actor_id=actor_id, cycle_id=param("cycle_id"),
                                            metric_id=metric_id)
        if method == "GET" and parsed.path == "/quality/scorecards":
            return 200, service.get_scorecard(actor_id=actor_id, cycle_id=param("cycle_id"),
                                              site_id=param("site_id"))
        if method == "GET" and parsed.path == "/quality/trace":
            return 200, service.trace_score(actor_id=actor_id, cycle_id=param("cycle_id"),
                                            site_id=param("site_id"), metric_id=param("metric_id"),
                                            purpose=param("purpose"))
        if method == "GET" and parsed.path == "/quality/exclusions":
            return 200, {"items": service.list_exclusions(actor_id=actor_id,
                                                          cycle_id=param("cycle_id"))}
        if method == "GET" and parsed.path == "/quality/disputes":
            return 200, {"items": service.list_disputes(actor_id=actor_id,
                                                        cycle_id=param("cycle_id"))}
        if method == "GET" and parsed.path == "/quality/plans":
            site_id = query.get("site_id", [None])[0]
            return 200, {"items": service.list_plans(actor_id=actor_id, site_id=site_id)}
        if method == "GET" and parsed.path == "/quality/release-decisions":
            return 200, {"items": service.list_release_decisions(actor_id=actor_id,
                                                                 site_id=param("site_id"))}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为质量对标路由调用。"""

    service: QualityService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动质量对标平台 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动跨工厂质量对标平台服务")
    parser.add_argument("--database", default="quality_benchmark.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = QualityDatabase(args.database)
    Handler.service = QualityService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
