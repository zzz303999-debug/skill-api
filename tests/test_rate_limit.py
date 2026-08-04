"""请求限流（内存滑动窗口）测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import rate_limit
from app.main import _LIMITERS, app

client = TestClient(app)


# ---------- SlidingWindowLimiter 单元测试 ----------


def test_limiter_allows_up_to_limit():
    limiter = rate_limit.SlidingWindowLimiter(max_requests=3, window_seconds=60)
    assert limiter.allow("ip-a") == (True, 0.0)
    assert limiter.allow("ip-a") == (True, 0.0)
    assert limiter.allow("ip-a") == (True, 0.0)
    allowed, retry_after = limiter.allow("ip-a")
    assert allowed is False
    assert retry_after > 0


def test_limiter_keys_are_independent():
    limiter = rate_limit.SlidingWindowLimiter(max_requests=2, window_seconds=60)
    assert limiter.allow("ip-a")[0] is True
    assert limiter.allow("ip-a")[0] is True
    assert limiter.allow("ip-a")[0] is False
    assert limiter.allow("ip-b")[0] is True


def test_limiter_window_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])

    limiter = rate_limit.SlidingWindowLimiter(max_requests=1, window_seconds=10)
    assert limiter.allow("ip-a")[0] is True
    assert limiter.allow("ip-a")[0] is False

    clock[0] += 10.0  # 窗口滑过
    assert limiter.allow("ip-a")[0] is True


def test_limiter_evicts_oldest_keys(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rate_limit, "_MAX_KEYS", 4)

    limiter = rate_limit.SlidingWindowLimiter(max_requests=1, window_seconds=60)
    for i in range(4):
        limiter.allow(f"ip-{i}")
    # 第 5 个 key 触发清理：最旧一半被移除，但新 key 仍可用
    assert limiter.allow("ip-4")[0] is True
    assert len(limiter._hits) <= 4


# ---------- 路径分类 ----------


def test_classify_path_heavy():
    assert rate_limit.classify_path("/orders") == "heavy"
    assert rate_limit.classify_path("/orders/parse-document") == "heavy"
    assert rate_limit.classify_path("/skills/tuoshu/extract") == "heavy"
    assert rate_limit.classify_path("/skills/tuoshu/batch-extract") == "heavy"


def test_classify_path_light():
    assert rate_limit.classify_path("/api/logs") == "light"


def test_classify_path_free():
    for path in ("/healthz", "/skills", "/logs", "/docs", "/openapi.json", "/favicon.ico"):
        assert rate_limit.classify_path(path) is None


def test_classify_path_unknown_not_limited():
    assert rate_limit.classify_path("/unknown/route") is None


# ---------- 429 响应 ----------


def test_build_rate_limited_response():
    response = rate_limit.build_rate_limited_response(2.3)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "3"
    body = response.body
    # JSONResponse 紧凑序列化（无空格）
    assert b'"code":"rate_limited"' in body


# ---------- 中间件（HTTP 层） ----------


def test_rate_limit_blocks_heavy_after_threshold(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(_LIMITERS["heavy"], "max_requests", 2)
    monkeypatch.setattr(_LIMITERS["heavy"], "_hits", {})
    monkeypatch.setattr(_LIMITERS["light"], "max_requests", 100)
    monkeypatch.setattr(_LIMITERS["light"], "_hits", {})

    # 无需真实文件：FastAPI 参数校验（422）也会经过中间件
    assert client.post("/orders/parse-document").status_code in (200, 422)
    assert client.post("/orders/parse-document").status_code in (200, 422)
    third = client.post("/orders/parse-document")
    assert third.status_code == 429
    # 窗口 60s：最早一次请求刚被记录，需等待近整个窗口
    assert third.headers["Retry-After"] == "60"
    assert third.json()["error"]["code"] == "rate_limited"


def test_rate_limit_records_access_log(monkeypatch):
    captured = []
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(_LIMITERS["heavy"], "max_requests", 1)
    monkeypatch.setattr(_LIMITERS["heavy"], "_hits", {})
    monkeypatch.setattr(main_module.access_log, "record", lambda entry: captured.append(entry))

    client.post("/orders/parse-document")  # 第 1 次放行
    response = client.post("/orders/parse-document")  # 第 2 次 429
    assert response.status_code == 429
    # 429 请求仍被 access_log 记录（审计留痕）
    assert captured
    assert captured[-1]["status"] == 429
    assert captured[-1]["error_code"] == "rate_limited"


def test_rate_limit_free_paths_not_limited(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(_LIMITERS["heavy"], "max_requests", 1)
    monkeypatch.setattr(_LIMITERS["heavy"], "_hits", {})
    monkeypatch.setattr(_LIMITERS["light"], "max_requests", 1)
    monkeypatch.setattr(_LIMITERS["light"], "_hits", {})

    for _ in range(5):
        assert client.get("/healthz").status_code == 200
        assert client.get("/skills").status_code == 200
        assert client.get("/logs").status_code == 200


def test_rate_limit_whitelist_exempts_ip(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(main_module, "_rate_limit_whitelist", frozenset({"testclient"}))
    monkeypatch.setattr(_LIMITERS["heavy"], "max_requests", 0)
    monkeypatch.setattr(_LIMITERS["heavy"], "_hits", {})

    # TestClient 的 client host 为 "testclient"，命中白名单 → 不拦截
    response = client.post("/orders/parse-document")
    assert response.status_code != 429


def test_rate_limit_disabled_passes_through(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "rate_limit_enabled", False)
    monkeypatch.setattr(_LIMITERS["heavy"], "max_requests", 0)
    monkeypatch.setattr(_LIMITERS["heavy"], "_hits", {})

    response = client.post("/orders/parse-document")
    assert response.status_code != 429
