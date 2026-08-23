"""Fake OTel SDK: emits OTLP/HTTP JSON or protobuf to a receiver.

Scenarios:
  basic      -> 2 metrics, 2 spans, stable attributes
  verbose    -> 5 metrics, 5 spans (the "bloaty PR")
  highcard   -> N metrics/spans with unique user_id values (unbounded dimension)
  basic-proto-> same as basic but binary protobuf
"""

import argparse
import json
import sys
import time
import urllib.request

sys.path.insert(0, __import__("os").path.join(
    __import__("os").path.dirname(__file__), "..", "src"))

from otel_budget_check import proto  # noqa: E402

BASE_URL = "http://127.0.0.1:%d/v1/%s"


def post(url, data, ctype):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200, r.read()


def attr(key, value):
    if isinstance(value, str):
        v = {"stringValue": value}
    elif isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    else:
        v = {"doubleValue": value}
    return {"key": key, "value": v}


def metric(name, attrs, kind="sum", value=1):
    return {
        "name": name,
        "description": "",
        kind: {"dataPoints": [{
            "attributes": attrs,
            "startTimeUnixNano": "1000",
            "timeUnixNano": "2000",
            "asInt": str(value),
        }]},
    }


def span(name, attrs, kind=2):
    return {
        "traceId": "0" * 32,
        "spanId": "0" * 16,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": "1000",
        "endTimeUnixNano": "2000",
        "attributes": attrs,
    }


def payload_metrics(metrics, resource_attrs=None):
    return {
        "resourceMetrics": [{
            "resource": {"attributes": resource_attrs or [attr("service.name", "demo")]},
            "scopeMetrics": [{
                "scope": {"name": "demo-scope"},
                "metrics": metrics,
            }],
        }],
    }


def payload_spans(spans, resource_attrs=None):
    return {
        "resourceSpans": [{
            "resource": {"attributes": resource_attrs or [attr("service.name", "demo")]},
            "scopeSpans": [{
                "scope": {"name": "demo-scope"},
                "spans": spans,
            }],
        }],
    }


def emit_json(port, scenario, count):
    base_metrics = [
        metric("http.server.request.duration", [attr("route", "/api/orders"), attr("method", "GET")],
               kind="histogram"),
        metric("http.server.active_requests", [attr("route", "/api/orders")]),
    ]
    base_spans = [
        span("GET /api/orders", [attr("http.route", "/api/orders"), attr("http.status_code", 200)]),
        span("db.query", [attr("db.system", "postgres"), attr("db.operation", "SELECT")]),
    ]
    if scenario == "basic":
        metrics, spans = base_metrics, base_spans
    elif scenario == "verbose":
        metrics = base_metrics + [
            metric("http.server.request.duration", [attr("route", "/api/orders"), attr("method", "POST")],
                   kind="histogram"),
            metric("http.server.request.size", [attr("route", "/api/orders")]),
            metric("cache.hit.ratio", [attr("cache", "redis")]),
        ]
        spans = base_spans + [
            span("POST /api/orders", [attr("http.route", "/api/orders"), attr("http.status_code", 201)]),
            span("cache.get", [attr("cache", "redis"), attr("cache.hit", True)]),
            span("queue.publish", [attr("queue", "orders"), attr("queue.priority", 1)]),
        ]
    elif scenario == "highcard":
        metrics = [metric("http.server.request.duration",
                          [attr("route", "/api/orders"), attr("user_id", "user-%d" % i)])
                   for i in range(count)]
        spans = [span("GET /api/orders", [attr("http.route", "/api/orders"),
                                          attr("user_id", "user-%d" % i)])
                 for i in range(count)]
    else:
        raise ValueError(scenario)
    post(BASE_URL % (port, "metrics"), json.dumps(payload_metrics(metrics)).encode(),
         "application/json")
    post(BASE_URL % (port, "traces"), json.dumps(payload_spans(spans)).encode(),
         "application/json")


def _enc_varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def _enc_len(b):
    return _enc_varint(len(b)) + b


def _field(fnum, wire, payload):
    return _enc_varint((fnum << 3) | wire) + payload


def _enc_attr(key, sval):
    kv = _field(1, 2, _enc_len(key.encode()))
    av = _field(1, 2, _enc_len(sval.encode()))
    kv += _field(2, 2, _enc_len(av))
    return _field(1, 2, _enc_len(kv))


def _enc_metric(name, value):
    m = _field(1, 2, _enc_len(name.encode()))
    dp = _field(1, 2, _enc_len(_enc_attr("route", "/api/orders")))
    dp += _field(2, 0, _enc_varint(1000))  # startTimeUnixNano
    dp += _field(3, 0, _enc_varint(2000))  # timeUnixNano
    dp += _field(6, 0, _enc_varint(value))  # asInt
    sum_msg = _field(1, 2, _enc_len(dp))  # dataPoints
    sum_msg += _field(2, 0, _enc_varint(1))  # aggregationTemporality
    sum_msg += _field(3, 0, _enc_varint(0))  # isMonotonic
    m += _field(7, 2, _enc_len(sum_msg))  # sum
    return m


def _enc_span(name):
    s = _field(1, 2, _enc_len(bytes(16)))   # traceId
    s += _field(2, 2, _enc_len(bytes(8)))   # spanId
    s += _field(5, 2, _enc_len(name.encode()))
    s += _field(6, 0, _enc_varint(2))       # kind
    s += _field(7, 0, _enc_varint(1000))    # start
    s += _field(8, 0, _enc_varint(2000))    # end
    s += _field(9, 2, _enc_len(_enc_attr("http.route", "/api/orders")))
    return s


def emit_proto(port):
    # Resource message: field 1 = repeated attributes.
    resource = _field(1, 2, _enc_len(_enc_attr("service.name", "demo")))
    # InstrumentationScope message: field 1 = name.
    scope = _field(1, 2, _enc_len("demo-scope".encode()))
    # ScopeMetrics: field 1 = scope, field 2 = repeated metrics.
    sm = _field(1, 2, _enc_len(scope))
    sm += _field(2, 2, _enc_len(_enc_metric("http.server.active_requests", 1)))
    sm += _field(2, 2, _enc_len(_enc_metric("db.queries", 1)))
    # ResourceMetrics: field 1 = resource, field 2 = repeated scopeMetrics.
    rm = _field(1, 2, _enc_len(resource))
    rm += _field(2, 2, _enc_len(sm))
    # ExportMetricsServiceRequest: field 1 = repeated resourceMetrics.
    metrics_req = _field(1, 2, _enc_len(rm))

    # ScopeSpans: field 1 = scope, field 2 = repeated spans.
    ss = _field(1, 2, _enc_len(scope))
    ss += _field(2, 2, _enc_len(_enc_span("GET /api/orders")))
    ss += _field(2, 2, _enc_len(_enc_span("db.query")))
    # ResourceSpans: field 1 = resource, field 2 = repeated scopeSpans.
    rs = _field(1, 2, _enc_len(resource))
    rs += _field(2, 2, _enc_len(ss))
    # ExportTraceServiceRequest: field 1 = repeated resourceSpans.
    traces_req = _field(1, 2, _enc_len(rs))

    post(BASE_URL % (port, "metrics"), metrics_req, "application/x-protobuf")
    post(BASE_URL % (port, "traces"), traces_req, "application/x-protobuf")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--scenario", default="basic")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()
    if args.scenario == "basic-proto":
        emit_proto(args.port)
    else:
        emit_json(args.port, args.scenario, args.count)
    time.sleep(0.2)  # let receiver finish reading before process exit


if __name__ == "__main__":
    main()