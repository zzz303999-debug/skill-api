"""托书抽取接口统一响应外壳契约测试。

2026-08-31 起 /skills/tuoshu/extract 全场景（成功/业务失败/请求错误）统一为
{code, msg, data}（对齐账单导入 v2.2 口径）。本文件集中验证该契约：

- 接口测试：HTTP 状态码 + 外壳三字段形状，覆盖中间件（401/413/429）、
  框架（422/500/503）、业务异常（400/422/502）各层错误路径
- 集成测试：TestClient 全链路 + mock 转换/LLM，验证成功响应 data 内
  {skill, version, result, meta, content} 与原顶层字段迁移一致

路径白名单见 app/main.py `_UNIFIED_RESPONSE_PATHS`；本文件任一场景变红
即说明白名单/异常处理器/中间件分流被破坏（退回 {error: ...} 或
{"detail": ...} 旧结构）。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.core import skill_registry
from app.core.errors import ConvertError, ParseError
from app.main import _LIMITERS, app
from app.skills.tuoshu import skill as skill_module

client = TestClient(app)


def _assert_unified_error(response, *, status: int, code: str):
    """统一外壳错误断言：HTTP 状态码 + 恰好三字段（code 机器可读、msg 可展示）。

    data 为补充详情（原 details；无详情时为 None）。set(body) 精确校验
    「只有 code/msg/data 三字段」——防止旧 error 结构混入。
    """
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == code
    assert body["msg"]


# ---------- 接口测试：错误路径统一外壳矩阵 ----------


def test_400_bad_request_unsupported_extension():
    """业务校验：扩展名不支持 → 400 bad_request（SkillAPIError 分流）。"""
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.txt", b"hi", "text/plain")},
    )
    _assert_unified_error(resp, status=400, code="bad_request")
    assert resp.json()["data"]["accepts"]  # 原 details 并入 data


def test_400_empty_file():
    """业务校验：空文件 → 400 empty_file（读取阶段校验）。"""
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    _assert_unified_error(resp, status=400, code="empty_file")


def test_401_unauthorized(monkeypatch):
    """鉴权中间件：配置 api_key 后未带凭证 → 401 unauthorized。"""
    monkeypatch.setattr(skill_module.settings, "api_key", "test-secret-key")
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"x", "application/octet-stream")},
    )
    _assert_unified_error(resp, status=401, code="unauthorized")


def test_413_payload_too_large(monkeypatch):
    """传输层：Content-Length 超限 → 413 payload_too_large（不进 handler）。"""
    monkeypatch.setattr(skill_module.settings, "api_max_upload_bytes", 3)
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"four", "application/octet-stream")},
    )
    _assert_unified_error(resp, status=413, code="payload_too_large")


def test_422_validation_missing_file():
    """框架校验：缺 file 字段 → 422 bad_request（RequestValidationError 分流）。"""
    resp = client.post("/skills/tuoshu/extract")
    _assert_unified_error(resp, status=422, code="bad_request")
    assert resp.json()["data"]["errors"]  # data 携带字段错误明细


def test_429_rate_limited(monkeypatch):
    """限流中间件：heavy 档超限 → 429 rate_limited（含 Retry-After 头）。"""
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(_LIMITERS["heavy"], "max_requests", 1)
    monkeypatch.setattr(_LIMITERS["heavy"], "_hits", {})
    monkeypatch.setattr(_LIMITERS["light"], "max_requests", 100)
    monkeypatch.setattr(_LIMITERS["light"], "_hits", {})
    client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"a", "application/octet-stream")},
    )  # 第 1 次放行（限流计数先于业务校验）
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"a", "application/octet-stream")},
    )  # 第 2 次 429
    _assert_unified_error(resp, status=429, code="rate_limited")
    assert resp.headers["Retry-After"]


def test_500_internal_error(monkeypatch):
    """未包装异常兜底：skill.run 抛 RuntimeError → 500 internal_error。

    TestClient 默认 raise_server_exceptions=True（收到 500 直接 re-raise 服务器
    异常以便定位），断言 500 响应体需用 False 关闭该保护。
    """
    skill = skill_registry.get("tuoshu")
    async def _boom(**_):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill, "run", _boom)
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"x", "application/octet-stream")},
    )
    _assert_unified_error(resp, status=500, code="internal_error")


def test_503_server_busy(monkeypatch):
    """并发控制：在途任务满载且排队超时 → 503 server_busy。"""
    import app.main as main_module

    async def _never_acquire():
        await asyncio.sleep(3600)

    monkeypatch.setattr(main_module._inflight_semaphore, "acquire", _never_acquire)
    monkeypatch.setattr(main_module.settings, "skill_queue_wait_seconds", 0.05)
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"x", "application/octet-stream")},
    )
    _assert_unified_error(resp, status=503, code="server_busy")


# ---------- 集成测试：成功全链路 + 业务失败链路 ----------


def _stub_llm_success(monkeypatch):
    """mock 文档转换与 LLM：返回最小合法 TuoshuOutput 数据（零网络）。"""
    markdown = "提单号：HLCUSHA12345678\n承运人：HMM\n做箱工厂：某门点\n柜1备注：博特装柜"
    monkeypatch.setattr(skill_module, "convert_to_markdown", lambda *_: markdown)

    async def fake_achat_json(_messages, **_kwargs):
        return (
            {
                "mbl_no": "HLCUSHA12345678",
                "carrier": "HMM",
                "shipper_company": "某托运人公司",
                "factory": {"name": "某门点"},
                "containers": [{"type": "40HC", "qty": 1, "remark": None}],
                "remark": "柜1备注：博特装柜",
                "raw_text_snippet": "不应采用的模型摘要",
                "source": {},
            },
            {"model": "fake", "usage": None},
        )

    monkeypatch.setattr(skill_module, "achat_json", fake_achat_json)


def test_success_unified_envelope(monkeypatch):
    """集成：TestClient 全链路（mock 转换/LLM）→ 200 统一外壳。

    原顶层字段迁入 data：data.result=抽取结果、data.meta=溯源、data.content=原文。
    """
    _stub_llm_success(monkeypatch)
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("order.docx", b"fake-docx", "application/octet-stream")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == "200"
    assert body["msg"] == "解析成功"
    data = body["data"]
    assert data["skill"] == "tuoshu"
    assert data["version"] == "0.1.0"
    assert data["content"].startswith("提单号：HLCUSHA12345678")
    assert data["meta"]["model"] == "fake"
    assert data["meta"]["source_bytes"] == len(b"fake-docx")
    result = data["result"]
    assert result["mbl_no"] == "HLCUSHA12345678"
    assert result["carrier"] == "HMM"
    assert result["containers"][0]["remark"] == "博特装柜"


def test_convert_error_422(monkeypatch):
    """集成：文档转换失败（ConvertError）→ 422 convert_error 统一外壳。"""
    monkeypatch.setattr(
        skill_module,
        "convert_to_markdown",
        lambda *_: (_ for _ in ()).throw(ConvertError("convert boom")),
    )
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("order.docx", b"fake-docx", "application/octet-stream")},
    )
    _assert_unified_error(resp, status=422, code="convert_error")


def test_parse_error_502(monkeypatch):
    """集成：LLM 输出不合法（ParseError）→ 502 parse_error 统一外壳。"""
    monkeypatch.setattr(skill_module, "convert_to_markdown", lambda *_: "markdown")

    async def fake_achat_json_error(_messages, **_kwargs):
        raise ParseError("llm boom")

    monkeypatch.setattr(skill_module, "achat_json", fake_achat_json_error)
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("order.docx", b"fake-docx", "application/octet-stream")},
    )
    _assert_unified_error(resp, status=502, code="parse_error")
