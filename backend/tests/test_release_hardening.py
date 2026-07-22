from app.core.observability import Metrics, safe_request_id


def test_request_ids_are_bounded_and_safe():
    assert safe_request_id("release-check-123") == "release-check-123"
    assert safe_request_id("too short") is None
    assert safe_request_id("bad\nvalue") is None


def test_metrics_render_prometheus_safe_labels():
    registry = Metrics()
    registry.increment("raikou_upload_failures_total", {"reason": "checksum"})
    registry.observe("raikou_qdrant_latency_seconds", 0.125)
    rendered = registry.render()
    assert 'raikou_upload_failures_total{reason="checksum"} 1.0' in rendered
    assert "raikou_qdrant_latency_seconds_count 1" in rendered
