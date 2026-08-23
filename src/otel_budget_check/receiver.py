"""OTLP receiver: captures telemetry emitted during a test run.

Speaks OTLP/HTTP (JSON and binary protobuf) on /v1/metrics and /v1/traces,
plus a /healthz probe. Zero dependencies (stdlib http.server only).
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import proto

CONTENT_JSON = "application/json"
CONTENT_PROTO = "application/x-protobuf"


class Capture:
    """Thread-safe accumulator for everything the receiver sees."""

    def __init__(self):
        self.lock = threading.Lock()
        self.metrics = []   # list of ResourceMetrics dicts
        self.spans = []     # list of ResourceSpans dicts
        self.counts = {"metrics": 0, "spans": 0, "requests": 0}
        self.errors = []

    def add_metrics(self, payload):
        with self.lock:
            self.metrics.append(payload)
            self.counts["metrics"] += _count_metric_points(payload)

    def add_spans(self, payload):
        with self.lock:
            self.spans.append(payload)
            self.counts["spans"] += _count_spans(payload)

    def snapshot(self):
        with self.lock:
            return {
                "metrics": list(self.metrics),
                "spans": list(self.spans),
                "counts": dict(self.counts),
                "errors": list(self.errors),
            }


def _count_metric_points(payload):
    total = 0
    for rm in payload.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                total += _points_in_metric(m)
    return total


def _points_in_metric(m):
    for kind in ("gauge", "sum", "histogram", "exponentialHistogram", "summary"):
        data = m.get(kind)
        if not data:
            continue
        points = data.get("dataPoints", [])
        if kind == "histogram":
            return sum(1 + len(p.get("buckets", [])) for p in points)
        if kind == "exponentialHistogram":
            return sum(1 + len(p.get("positive", {}).get("buckets", []))
                       + len(p.get("negative", {}).get("buckets", []))
                       for p in points)
        return len(points)
    return 0


def _count_spans(payload):
    total = 0
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            total += len(ss.get("spans", []))
    return total


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _decode(self, raw, content_type):
        if content_type.startswith(CONTENT_PROTO):
            return self._proto_to_json(raw)
        if content_type.startswith(CONTENT_JSON):
            return json.loads(raw.decode("utf-8"))
        # Some SDKs send protobuf without a content type; try JSON, then proto.
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return self._proto_to_json(raw)

    def _proto_to_json(self, raw):
        # Decode ExportMetricsServiceRequest / ExportTraceServiceRequest.
        # Both have the payload at field 1; disambiguate by trying metrics
        # first, then spans (a metrics parse of span bytes yields 0 points).
        try:
            top = proto.decode_message(raw)
        except Exception:
            raise ValueError("unrecognized OTLP payload")
        if not (1 in top and top[1]):
            raise ValueError("unrecognized OTLP payload")
        try:
            metrics = {"resourceMetrics": [_rm_to_json(b) for b in top[1]]}
            if _count_metric_points(metrics) > 0:
                return metrics
        except Exception:
            pass
        try:
            spans = {"resourceSpans": [_rs_to_json(b) for b in top[1]]}
            if _count_spans(spans) > 0:
                return spans
        except Exception:
            pass
        raise ValueError("unrecognized OTLP payload")

    def _send(self, code, body=b"", ctype=CONTENT_JSON):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        cap = self.server.capture  # type: ignore[attr-defined]
        cap.counts["requests"] += 1
        raw = self._read_body()
        ctype = self.headers.get("Content-Type", "")
        try:
            payload = self._decode(raw, ctype)
            if self.path.rstrip("/").endswith("/v1/metrics"):
                cap.add_metrics(payload)
            elif self.path.rstrip("/").endswith("/v1/traces"):
                cap.add_spans(payload)
            else:
                cap.errors.append("unknown path: %s" % self.path)
                self._send(404, b'{"error":"unknown path"}')
                return
            self._send(200, b"{}")
        except Exception as exc:  # noqa: BLE001 - receiver must not die
            cap.errors.append("%s: %s" % (self.path, exc))
            self._send(400, json.dumps({"error": str(exc)}).encode("utf-8"))

    def do_GET(self):
        if self.path.rstrip("/").endswith("/healthz"):
            self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"error":"not found"}')


class OTLPReceiver:
    def __init__(self, host="127.0.0.1", port=0):
        self.capture = Capture()
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.capture = self.capture  # type: ignore[attr-defined]
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def endpoint(self, kind):
        return "http://127.0.0.1:%d/v1/%s" % (self.port, kind)


def _rm_to_json(raw):
    out = {}
    fields = proto.decode_message(raw)
    if 1 in fields:  # resource
        resource = proto.decode_message(fields[1][0])
        attrs = _attrs_to_json(resource.get(1, []))
        if attrs:
            out["resource"] = {"attributes": attrs}
    out["scopeMetrics"] = []
    for sm in fields.get(2, []):
        smd = proto.decode_message(sm)
        scope = proto.decode_message(smd[1][0]) if 1 in smd else {}
        entry = {
            "scope": {"name": _first_str(scope, 1, "unknown")},
            "metrics": [_metric_to_json(m) for m in smd.get(2, [])],
        }
        out["scopeMetrics"].append(entry)
    return out


def _metric_to_json(raw):
    fields = proto.decode_message(raw)
    m = {"name": _first_str(fields, 1, ""), "description": _first_str(fields, 2, "")}
    if 5 in fields:  # gauge (Gauge.dataPoints = field 1)
        points = []
        for g in fields[5]:
            gf = proto.decode_message(g)
            points.extend(_dp_to_json(p) for p in gf.get(1, []))
        m["gauge"] = {"dataPoints": points}
    if 7 in fields:  # sum (Sum.dataPoints = field 1)
        points = []
        for s in fields[7]:
            sf = proto.decode_message(s)
            points.extend(_dp_to_json(p) for p in sf.get(1, []))
        m["sum"] = {"dataPoints": points}
    if 9 in fields:  # histogram (Histogram.dataPoints = field 1)
        points = []
        for h in fields[9]:
            hf = proto.decode_message(h)
            points.extend(_hist_to_json(p) for p in hf.get(1, []))
        m["histogram"] = {"dataPoints": points}
    return m


def _dp_to_json(raw):
    fields = proto.decode_message(raw)
    dp = {}
    if 1 in fields:  # attributes
        dp["attributes"] = _attrs_to_json(fields[1])
    if 2 in fields:  # start time unixnano
        dp["startTimeUnixNano"] = str(fields[2][0])
    if 3 in fields:  # time unixnano
        dp["timeUnixNano"] = str(fields[3][0])
    for fnum, key in ((4, "asDouble"), (6, "asInt")):
        if fnum in fields:
            dp[key] = fields[fnum][0]
    return dp


def _hist_to_json(raw):
    fields = proto.decode_message(raw)
    dp = _dp_to_json(raw)
    if 4 in fields:  # count
        dp["count"] = str(fields[4][0])
    if 5 in fields:  # sum
        dp["sum"] = proto.f64(fields[5][0])
    if 6 in fields:  # bucket counts
        dp["bucketCounts"] = [str(c) for c in fields[6]]
    if 7 in fields:  # explicit bounds
        dp["explicitBounds"] = [proto.f64(b) for b in fields[7]]
    return dp


def _rs_to_json(raw):
    fields = proto.decode_message(raw)
    out = {}
    if 1 in fields:
        resource = proto.decode_message(fields[1][0])
        attrs = _attrs_to_json(resource.get(1, []))
        if attrs:
            out["resource"] = {"attributes": attrs}
    out["scopeSpans"] = []
    for ss in fields.get(2, []):
        ssd = proto.decode_message(ss)
        scope = proto.decode_message(ssd[1][0]) if 1 in ssd else {}
        entry = {
            "scope": {"name": _first_str(scope, 1, "unknown")},
            "spans": [_span_to_json(s) for s in ssd.get(2, [])],
        }
        out["scopeSpans"].append(entry)
    return out


def _span_to_json(raw):
    fields = proto.decode_message(raw)
    span = {
        "traceId": _first_bytes_hex(fields, 1),
        "spanId": _first_bytes_hex(fields, 2),
        "name": _first_str(fields, 5, ""),
        "kind": fields.get(6, [0])[0],
        "startTimeUnixNano": str(fields.get(7, [0])[0]),
        "endTimeUnixNano": str(fields.get(8, [0])[0]),
    }
    if 9 in fields:  # attributes
        span["attributes"] = _attrs_to_json(fields[9])
    for fnum, key in ((10, "droppedAttributesCount"),
                      (11, "events"), (12, "droppedEventsCount"),
                      (13, "links"), (14, "droppedLinksCount")):
        if fnum in fields and key in ("droppedAttributesCount",
                                      "droppedEventsCount", "droppedLinksCount"):
            span[key] = fields[fnum][0]
    if 15 in fields:  # status
        status = proto.decode_message(fields[15][0])
        span["status"] = {"code": status.get(3, [0])[0]}
    return span


def _attrs_to_json(raw_list):
    out = []
    for raw in raw_list:
        kv = proto.decode_message(raw)
        key = _first_str(kv, 1, "")
        value = proto.decode_message(kv[2][0]) if 2 in kv else {}
        out.append({"key": key, "value": _anyvalue_to_json(value)})
    return out


def _anyvalue_to_json(value):
    for fnum, key in ((1, "stringValue"), (2, "boolValue"), (3, "intValue"),
                      (4, "doubleValue"), (5, "arrayValue"), (6, "kvlistValue"),
                      (7, "bytesValue")):
        if fnum not in value:
            continue
        raw = value[fnum][0]
        if key == "stringValue":
            return {"stringValue": raw.decode("utf-8", "replace")}
        if key == "boolValue":
            return {"boolValue": bool(raw)}
        if key == "intValue":
            return {"intValue": str(raw)}
        if key == "doubleValue":
            return {"doubleValue": proto.f64(raw)}
        if key == "bytesValue":
            return {"bytesValue": raw.hex()}
        if key == "arrayValue":
            arr = proto.decode_message(raw)
            return {"arrayValue": {"values": [_anyvalue_to_json(proto.decode_message(v))
                                              for v in arr.get(1, [])]}}
        if key == "kvlistValue":
            kvl = proto.decode_message(raw)
            return {"kvlistValue": {"values": _attrs_to_json(kvl.get(1, []))}}
    return {}


def _first_str(fields, fnum, default):
    if fnum in fields and fields[fnum]:
        return fields[fnum][0].decode("utf-8", "replace")
    return default


def _first_bytes_hex(fields, fnum):
    if fnum in fields and fields[fnum]:
        return fields[fnum][0].hex()
    return ""


def main():
    import argparse

    parser = argparse.ArgumentParser(description="OTLP capture receiver")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--out", default="", help="write capture JSON to this file on exit")
    args = parser.parse_args()

    recv = OTLPReceiver(port=args.port).start()
    print("OTLP receiver listening on 127.0.0.1:%d" % recv.port, flush=True)
    try:
        while True:
            import time
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        snap = recv.capture.snapshot()
        recv.stop()
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(snap, fh)
            print("wrote capture to %s" % args.out, flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()