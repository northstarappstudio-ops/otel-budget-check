"""Analyzer: baseline vs PR telemetry comparison + budget gate.

Produces a report dict with:
- added/removed metrics, span names, attribute keys
- observed cardinality change (unique series / unique span name+attrs)
- obviously unbounded dimensions (high-cardinality attribute values)
- projected signal-volume delta (test volume * traffic multiplier)
- estimated monthly cost delta (volume delta * backend unit cost)
- PASS/FAIL budget gate
"""

import math

# Defaults (overridable via action inputs)
DEFAULT_TRAFFIC_MULTIPLIER = 100.0
DEFAULT_UNIT_COST = 1.0          # $ per 1M signals (metrics points + spans)
DEFAULT_BUDGET = 50.0            # $/mo allowed increase
DEFAULT_CARDINALITY_LIMIT = 100  # unique values before flagged "unbounded"
DEFAULT_SAMPLE_INTERVAL = 5.0    # seconds of test telemetry assumed per run


def _iter_metric_points(payload):
    for rm in payload.get("resourceMetrics", []):
        res_attrs = _attr_map(rm.get("resource", {}).get("attributes", []))
        for sm in rm.get("scopeMetrics", []):
            scope = sm.get("scope", {}).get("name", "unknown")
            for m in sm.get("metrics", []):
                name = m.get("name", "")
                for kind in ("gauge", "sum", "histogram", "exponentialHistogram", "summary"):
                    data = m.get(kind)
                    if not data:
                        continue
                    for dp in data.get("dataPoints", []):
                        attrs = dict(res_attrs)
                        attrs.update(_attr_map(dp.get("attributes", [])))
                        yield {"metric": name, "kind": kind, "attributes": attrs}
                    break


def _iter_spans(payload):
    for rs in payload.get("resourceSpans", []):
        res_attrs = _attr_map(rs.get("resource", {}).get("attributes", []))
        for ss in rs.get("scopeSpans", []):
            scope = ss.get("scope", {}).get("name", "unknown")
            for s in ss.get("spans", []):
                attrs = dict(res_attrs)
                attrs.update(_attr_map(s.get("attributes", [])))
                yield {"name": s.get("name", ""), "kind": s.get("kind", 0),
                       "scope": scope, "attributes": attrs}


def _attr_map(attrs):
    out = {}
    for a in attrs or []:
        key = a.get("key", "")
        val = a.get("value", {})
        if "stringValue" in val:
            out[key] = val["stringValue"]
        elif "intValue" in val:
            out[key] = val["intValue"]
        elif "boolValue" in val:
            out[key] = "true" if val["boolValue"] else "false"
        elif "doubleValue" in val:
            out[key] = repr(val["doubleValue"])
        else:
            out[key] = "<complex>"
    return out


def _series_key(item):
    pairs = sorted(item["attributes"].items())
    flat = [v for kv in pairs for v in kv]
    return (item["metric"],) + tuple(flat)


def _span_series_key(item):
    pairs = sorted(item["attributes"].items())
    flat = [v for kv in pairs for v in kv]
    return (item["name"], item["scope"]) + tuple(flat)


def _collect(payload):
    """Return (metric_series, span_series, metric_points, spans, attr_value_counts)."""
    payload = _normalize(payload)
    metric_series = {}
    span_series = {}
    metric_points = 0
    spans = 0
    attr_counts = {}
    for item in _iter_metric_points(payload):
        metric_points += 1
        key = _series_key(item)
        metric_series[key] = metric_series.get(key, 0) + 1
        for k, v in item["attributes"].items():
            attr_counts.setdefault(k, {}).setdefault(v, 0)
            attr_counts[k][v] += 1
    for item in _iter_spans(payload):
        spans += 1
        key = _span_series_key(item)
        span_series[key] = span_series.get(key, 0) + 1
        for k, v in item["attributes"].items():
            attr_counts.setdefault(k, {}).setdefault(v, 0)
            attr_counts[k][v] += 1
    return metric_series, span_series, metric_points, spans, attr_counts


def _normalize(payload):
    """Accept either a raw OTLP payload or a capture snapshot."""
    if "resourceMetrics" in payload or "resourceSpans" in payload:
        return payload
    merged = {"resourceMetrics": [], "resourceSpans": []}
    for p in payload.get("metrics", []):
        merged["resourceMetrics"].extend(p.get("resourceMetrics", []))
    for p in payload.get("spans", []):
        merged["resourceSpans"].extend(p.get("resourceSpans", []))
    return merged


def _unbounded(attr_counts, limit):
    flagged = []
    for key, values in sorted(attr_counts.items()):
        if len(values) > limit:
            top = sorted(values.items(), key=lambda kv: -kv[1])[:5]
            flagged.append({
                "attribute": key,
                "unique_values": len(values),
                "sample_values": [v for v, _ in top],
            })
    return flagged


def analyze(baseline, current, traffic_multiplier=DEFAULT_TRAFFIC_MULTIPLIER,
            unit_cost=DEFAULT_UNIT_COST, budget=DEFAULT_BUDGET,
            cardinality_limit=DEFAULT_CARDINALITY_LIMIT,
            sample_interval=DEFAULT_SAMPLE_INTERVAL):
    b = _collect(baseline)
    c = _collect(current)
    b_metric_series, b_span_series, b_points, b_spans, _ = b
    c_metric_series, c_span_series, c_points, c_spans, _ = c

    added_metrics = sorted(set(c_metric_series) - set(b_metric_series))
    removed_metrics = sorted(set(b_metric_series) - set(c_metric_series))
    added_spans = sorted(set(c_span_series) - set(b_span_series))
    removed_spans = sorted(set(b_span_series) - set(c_span_series))

    b_attr_keys = {k for s in b_metric_series for k in s[1::2]} | \
                  {k for s in b_span_series for k in s[2::2]}
    c_attr_keys = {k for s in c_metric_series for k in s[1::2]} | \
                  {k for s in c_span_series for k in s[2::2]}
    added_attrs = sorted(c_attr_keys - b_attr_keys)
    removed_attrs = sorted(b_attr_keys - c_attr_keys)

    # Cardinality: unique series per signal type.
    b_card = len(b_metric_series) + len(b_span_series)
    c_card = len(c_metric_series) + len(c_span_series)
    cardinality_delta = c_card - b_card

    unbounded = _unbounded(c[4], cardinality_limit)

    # Volume projection: signals per second of test telemetry * multiplier.
    b_rate = (b_points + b_spans) / max(sample_interval, 1e-9)
    c_rate = (c_points + c_spans) / max(sample_interval, 1e-9)
    b_volume = b_rate * traffic_multiplier * 60 * 60 * 24 * 30
    c_volume = c_rate * traffic_multiplier * 60 * 60 * 24 * 30
    volume_delta = c_volume - b_volume

    b_cost = b_volume / 1_000_000 * unit_cost
    c_cost = c_volume / 1_000_000 * unit_cost
    cost_delta = c_cost - b_cost

    failures = []
    if cost_delta > budget:
        failures.append("estimated monthly cost delta $%.2f exceeds budget $%.2f"
                        % (cost_delta, budget))
    if unbounded:
        failures.append("%d potentially unbounded dimension(s): %s"
                        % (len(unbounded),
                           ", ".join(u["attribute"] for u in unbounded)))

    return {
        "added_metrics": [_fmt_series(s) for s in added_metrics],
        "removed_metrics": [_fmt_series(s) for s in removed_metrics],
        "added_spans": [_fmt_span(s) for s in added_spans],
        "removed_spans": [_fmt_span(s) for s in removed_spans],
        "added_attributes": added_attrs,
        "removed_attributes": removed_attrs,
        "cardinality": {
            "baseline_unique_series": b_card,
            "current_unique_series": c_card,
            "delta": cardinality_delta,
        },
        "unbounded_dimensions": unbounded,
        "volume": {
            "baseline_projected_monthly_signals": b_volume,
            "current_projected_monthly_signals": c_volume,
            "delta": volume_delta,
        },
        "cost": {
            "baseline_monthly": b_cost,
            "current_monthly": c_cost,
            "delta": cost_delta,
        },
        "gate": {
            "status": "FAIL" if failures else "PASS",
            "failures": failures,
        },
    }


def _fmt_series(series):
    # series = (name, k1, v1, k2, v2, ...)
    name = series[0]
    attrs = ", ".join("%s=%s" % (series[i], series[i + 1])
                      for i in range(1, len(series), 2))
    return "%s{%s}" % (name, attrs) if attrs else name


def _fmt_span(series):
    name = series[0]
    attrs = ", ".join("%s=%s" % (series[i], series[i + 1])
                      for i in range(2, len(series), 2))
    return "%s{%s}" % (name, attrs) if attrs else name


def format_report(report, verbose=False):
    lines = []
    lines.append("## otel-budget-check report")
    g = report["gate"]
    lines.append("**Gate: %s**" % g["status"])
    for f in g["failures"]:
        lines.append("- FAIL: %s" % f)
    lines.append("")
    lines.append("### Added / removed")
    lines.append("- metrics added: %d" % len(report["added_metrics"]))
    for s in report["added_metrics"][:20]:
        lines.append("  + %s" % s)
    lines.append("- metrics removed: %d" % len(report["removed_metrics"]))
    for s in report["removed_metrics"][:20]:
        lines.append("  - %s" % s)
    lines.append("- spans added: %d" % len(report["added_spans"]))
    for s in report["added_spans"][:20]:
        lines.append("  + %s" % s)
    lines.append("- spans removed: %d" % len(report["removed_spans"]))
    for s in report["removed_spans"][:20]:
        lines.append("  - %s" % s)
    lines.append("- attribute keys added: %s" % ", ".join(report["added_attributes"]) or "(none)")
    lines.append("- attribute keys removed: %s" % ", ".join(report["removed_attributes"]) or "(none)")
    lines.append("")
    card = report["cardinality"]
    lines.append("### Cardinality")
    lines.append("- baseline unique series: %d" % card["baseline_unique_series"])
    lines.append("- current unique series: %d" % card["current_unique_series"])
    lines.append("- delta: %+d" % card["delta"])
    if report["unbounded_dimensions"]:
        lines.append("- **unbounded dimensions:**")
        for u in report["unbounded_dimensions"]:
            lines.append("  - %s: %d unique values (sample: %s)"
                         % (u["attribute"], u["unique_values"],
                            ", ".join(map(str, u["sample_values"]))))
    lines.append("")
    vol = report["volume"]
    cost = report["cost"]
    lines.append("### Projected volume (test rate x traffic multiplier x 30d)")
    lines.append("- baseline: %.1fM signals/mo" % (vol["baseline_projected_monthly_signals"] / 1e6))
    lines.append("- current: %.1fM signals/mo" % (vol["current_projected_monthly_signals"] / 1e6))
    lines.append("- delta: %+.1fM signals/mo" % (vol["delta"] / 1e6))
    lines.append("")
    lines.append("### Estimated monthly cost")
    lines.append("- baseline: $%.2f" % cost["baseline_monthly"])
    lines.append("- current: $%.2f" % cost["current_monthly"])
    lines.append("- delta: %+.2f" % cost["delta"])
    if verbose:
        lines.append("")
        lines.append("### Raw series (current)")
        for s in sorted(report["added_metrics"] + report["added_spans"])[:50]:
            lines.append("- %s" % s)
    return "\n".join(lines)