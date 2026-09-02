"""skill 执行、HTTP API 与 chat 渲染行为测试。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.core import registry
from app.errors import ConvertError, ParseError
from app.main import app
from app.skills.tuoshu.chinese_schema import to_chinese
from app.skills.tuoshu.prompt import format_to_chat_text
from app.skills.tuoshu.skill import TuoshuSkill

client = TestClient(app)



def test_upload_limit(monkeypatch):
    monkeypatch.setattr(settings, "api_max_upload_bytes", 3)

    response = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"four", "application/octet-stream")},
    )

    # 传输层硬限制优先：Content-Length 超限直接 413，不进入 handler
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_empty_upload_is_rejected_before_conversion():
    response = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "empty_file"


def test_extract_missing_file_field_returns_unified_422():
    """缺 file 字段 → 422 套统一外壳（code=bad_request，data.errors 带字段明细）。"""
    response = client.post("/skills/tuoshu/extract")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "bad_request"
    assert body["msg"]
    assert body["data"]["errors"]
    assert any(e["loc"] == ["body", "file"] for e in body["data"]["errors"])


def test_batch_extract_calls_keyword_only_skill(monkeypatch):
    skill = registry.get("tuoshu")

    async def fake_run(*, file_bytes: bytes, filename: str, options=None):
        return {
            "result": {"filename": filename, "size": len(file_bytes)},
            "meta": {"fake": True},
        }

    monkeypatch.setattr(skill, "run", fake_run)
    response = client.post(
        "/skills/tuoshu/batch-extract",
        files=[
            ("files", ("a.xlsx", b"a", "application/octet-stream")),
            ("files", ("b.xlsx", b"bb", "application/octet-stream")),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] == 2
    assert body["failed"] == 0
    assert [item["result"]["filename"] for item in body["results"]] == ["a.xlsx", "b.xlsx"]


def test_extract_response_has_no_independent_summary(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    monkeypatch.setattr(
        skill_module,
        "convert_to_markdown",
        lambda _file_bytes, _filename: (
            "提单号：HLCUSHA12345678\n承运人：HMM\n柜1备注：博特装柜"
        ),
    )

    async def fake_achat_json(_messages, **_kwargs):
        return (
            {
                "mbl_no": "HLCUSHA12345678",
                "carrier": "HMM",
                "shipper_company": "某托运人公司",
                "factory": {"name": "某门点"},
                "containers": [{"type": "40HC", "qty": 1, "remark": None}],
                "remark": "柜1备注：博特装柜",
                "raw_text_snippet": "模型生成的摘要不得采用",
                "source": {},
            },
            {
                "model": "fake",
                "usage": None,
                "chat_text": "承运人: HMM\n做箱工厂: 错误工厂",
                "summary": "模型二次摘要",
            },
        )

    monkeypatch.setattr(skill_module, "achat_json", fake_achat_json)

    response = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("order.docx", b"fake-docx", "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    # 统一响应外壳：{code, msg, data}，data 内 {skill, version, result, meta, content}
    assert body["code"] == "200"
    assert body["msg"] == "解析成功"
    data = body["data"]
    assert data["content"] == "提单号：HLCUSHA12345678\n承运人：HMM\n柜1备注：博特装柜"
    assert data["meta"]["model"] == "fake"
    assert data["meta"]["usage"] is None
    assert data["meta"]["conversion_status"] == "converted"
    assert data["meta"]["source_bytes"] == len(b"fake-docx")
    assert data["meta"]["content_chars"] == len(data["content"])
    assert len(data["meta"]["source_sha256"]) == 64
    assert len(data["meta"]["content_sha256"]) == 64
    result = data["result"]
    assert "carrier" in result and "承运人" not in result
    assert result["carrier"] == "HMM"
    assert result["factory"]["name"] == "某门点"
    assert result["containers"][0]["remark"] == "博特装柜"
    assert result["raw_text_snippet"].startswith("提单号：HLCUSHA12345678")

    rendered = format_to_chat_text(result)
    assert "船公司: HMM（接口原始值）" in rendered
    assert "船公司: HLC（接口原始值）" not in rendered
    assert "做箱工厂: 某门点" in rendered
    assert "错误工厂" not in rendered
    assert "箱型备注: 博特装柜" in rendered


def test_scanned_pdf_is_sent_as_vision_pages(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    # 启用 LLM 视觉能力，验证扫描 PDF 转 vision 页面的路径
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    captured: dict = {}
    monkeypatch.setattr(
        skill_module,
        "convert_to_markdown",
        lambda _file_bytes, _filename: "SCAN_OR_IMAGE_HINT: scan.pdf",
    )
    monkeypatch.setattr(
        skill_module,
        "render_pdf_pages",
        lambda _file_bytes, *, max_pages, scale: [b"page-1", b"page-2"],
    )

    async def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "achat_json", fake_chat_json)

    response = asyncio.run(TuoshuSkill().run(file_bytes=b"fake-pdf", filename="scan.pdf"))

    user_content = captured["messages"][-1]["content"]
    images = [item for item in user_content if item["type"] == "image_url"]
    assert len(images) == 2
    assert all(item["image_url"]["url"].startswith("data:image/png;base64,") for item in images)
    assert response["content"] == ""
    assert response["meta"]["conversion_status"] == "needs_review"
    assert response["meta"]["page_routes"] == [
        {
            "page": 1,
            "parser": "vision",
            "confidence": "low",
            "issues": ["vision_only_unverified"],
        },
        {
            "page": 2,
            "parser": "vision",
            "confidence": "low",
            "issues": ["vision_only_unverified"],
        },
    ]
    assert any(
        issue["code"] == "vision_only_unverified" and issue["blocking"]
        for issue in response["result"]["review_issues"]
    )
    assert response["result"]["ready_for_order"] is False


def test_incomplete_review_issue_is_repaired_once(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    calls: list[list[dict]] = []

    async def fake_chat_json(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "review_issues": [
                    {
                        "code": "document_value_unclear",
                        "message": "单据字段无法辨认，需人工确认",
                    }
                ],
                "source": {},
            }, {"model": "fake", "usage": None}
        return {
            "review_issues": [
                {
                    "code": "document_value_unclear",
                    "field": "customer_ref",
                    "message": "单据字段无法辨认，需人工确认",
                    "source_values": [],
                    "blocking": True,
                }
            ],
            "source": {},
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "achat_json", fake_chat_json)

    # 启用 LLM 视觉能力（无视觉时该流程在 convert 阶段即被拒绝）
    monkeypatch.setattr(settings, "llm_vision_enabled", True)

    response = asyncio.run(
        TuoshuSkill().run(
            file_bytes=b"\x89PNG\r\n\x1a\nimage",
            filename="order.png",
        )
    )

    assert len(calls) == 2
    assert calls[1][-2]["role"] == "assistant"
    assert "index=0" in calls[1][-1]["content"]
    assert any(
        issue["code"] == "document_value_unclear"
        and issue["field"] == "customer_ref"
        for issue in response["result"]["review_issues"]
    )


def test_incomplete_review_issue_still_fails_after_one_repair(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    calls = 0

    async def fake_chat_json(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "review_issues": [{"message": "箱号无法辨认"}],
            "source": {},
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "achat_json", fake_chat_json)

    # 启用 LLM 视觉能力（无视觉时该流程在 convert 阶段即被拒绝）
    monkeypatch.setattr(settings, "llm_vision_enabled", True)

    with pytest.raises(ParseError, match="required structure"):
        asyncio.run(
            TuoshuSkill().run(
                file_bytes=b"\x89PNG\r\n\x1a\nimage",
                filename="order.png",
            )
        )

    assert calls == 2


def test_parser_fallback_issue_reaches_final_output(monkeypatch):
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseIssue, ParseResult

    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="vision",
                confidence="low",
                vision_image=b"\x89PNG\r\n\x1a\nimage",
                issues=[
                    ParseIssue(
                        code="mineru_low_confidence",
                        message="MinerU 图片结果低置信，已转 vision，必须人工复核",
                        page=1,
                        source_values=("no_table_or_key_labels",),
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)
    async def fake_achat_json(*_args, **_kwargs):
        return (
            {
                "shipper_company": "某托运人公司",
                "factory": {"name": "某门点"},
                "source": {},
            },
            {"model": "fake", "usage": None},
        )

    monkeypatch.setattr(skill_module, "achat_json", fake_achat_json)
    # 启用 LLM 视觉能力（无视觉时 parser=vision 且无 OCR 文本会被拒绝）
    monkeypatch.setattr(settings, "llm_vision_enabled", True)

    response = asyncio.run(
        TuoshuSkill().run(
            file_bytes=b"\x89PNG\r\n\x1a\nimage",
            filename="order.png",
        )
    )

    issue = next(
        item
        for item in response["result"]["review_issues"]
        if item["code"] == "mineru_low_confidence"
    )
    assert issue["blocking"] is True
    assert response["result"]["ready_for_order"] is False
    assert response["meta"]["page_routes"][0]["parser"] == "vision"


def test_high_confidence_mineru_image_skips_vision_for_speed(monkeypatch):
    """MinerU 高置信时跳过 LLM vision 交叉核验，只用 OCR 文本走 LLM 以提速。"""
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseResult

    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    parsed_text = "备注：出口清关的装完箱后请及时进港"
    captured: dict = {}
    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="mineru",
                markdown=parsed_text,
                vision_image=image_bytes,
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)

    async def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "achat_json", fake_chat_json)

    result = asyncio.run(TuoshuSkill().run(file_bytes=image_bytes, filename="order.png"))

    user_content = captured["messages"][-1]["content"]
    # 高置信 MinerU 跳过 LLM vision，走纯文本通道
    assert isinstance(user_content, str)
    assert parsed_text in user_content
    assert "image_url" not in user_content
    # 标注跳过了 vision 交叉核验，提示人工抽检关键字段
    issues = result["result"].get("review_issues", [])
    assert any(i["code"] == "vision_cross_check_skipped" for i in issues)


def test_low_confidence_mineru_image_keeps_vision_cross_check(monkeypatch):
    """MinerU 低置信/fallback 时仍携原图走 LLM vision 交叉核验。"""
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseResult

    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    parsed_text = "备注：出口清关的装完箱后请及时进港"
    captured: dict = {}
    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="vision",
                markdown=parsed_text,
                confidence="low",
                vision_image=image_bytes,
                vision_mime="image/png",
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)
    # 启用 LLM 视觉能力，验证低置信图片携原图走 vision 交叉核验
    monkeypatch.setattr(settings, "llm_vision_enabled", True)

    async def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "achat_json", fake_chat_json)

    asyncio.run(TuoshuSkill().run(file_bytes=image_bytes, filename="order.png"))

    user_content = captured["messages"][-1]["content"]
    text_content = next(item["text"] for item in user_content if item["type"] == "text")
    images = [item for item in user_content if item["type"] == "image_url"]
    assert parsed_text in text_content
    assert "OCR 中存在但图片上看不到的词句必须剔除" in text_content
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert images[0]["image_url"]["detail"] == "high"


def test_oversized_image_degrades_to_text_and_flags_manual_review(monkeypatch):
    """原图总大小超过 vision 直传上限时降级纯文本，并保留 blocking 复核标记。"""
    import app.skills.tuoshu.skill as skill_module
    from app.config import settings
    from app.document_parsers.models import ParsedPage, ParseResult

    parsed_text = "提单号：KMTCSHAP950393；船名：RESURGENCE"
    captured: dict = {}
    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="vision",
                markdown=parsed_text,
                confidence="low",
                vision_image=b"\x00" * (settings.vision_max_image_bytes + 1),
                vision_mime="image/png",
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)
    # 启用 LLM 视觉能力，验证超限降级路径
    monkeypatch.setattr(settings, "llm_vision_enabled", True)

    async def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "achat_json", fake_chat_json)

    result = asyncio.run(
        TuoshuSkill().run(
            file_bytes=b"\x89PNG\r\n\x1a\nimage", filename="order.png"
        )
    )

    user_content = captured["messages"][-1]["content"]
    # 超限降级为纯文本通道，不再携带 base64 图片
    assert isinstance(user_content, str)
    assert parsed_text in user_content
    assert "image_url" not in user_content
    # blocking 复核标记必须保留在后处理白名单中（vision_image_too_large）
    issues = result["result"].get("review_issues", [])
    issue = next(
        item for item in issues if item["code"] == "vision_image_too_large"
    )
    assert issue["blocking"] is True
    assert result["result"]["ready_for_order"] is False


def test_oversized_image_without_ocr_text_rejected(monkeypatch):
    """图片超限且无 OCR 文本（MinerU 未启用/失败）时拒绝，禁止空内容喂 LLM。"""
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseResult

    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="vision",
                confidence="low",
                # markdown 为空：等价于 mineru_enabled=False 或 MinerU 失败降级
                vision_image=b"\x00" * (settings.vision_max_image_bytes + 1),
                vision_mime="image/png",
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)
    # 启用 LLM 视觉能力，验证“超限且无 OCR”拒绝路径
    monkeypatch.setattr(settings, "llm_vision_enabled", True)

    with pytest.raises(ConvertError) as exc_info:
        asyncio.run(
            TuoshuSkill().run(
                file_bytes=b"\x89PNG\r\n\x1a\nimage", filename="order.png"
            )
        )
    assert exc_info.value.code == "vision_image_too_large"


def test_image_without_ocr_text_rejected_when_vision_disabled(monkeypatch):
    """默认无视觉模型：图片无 OCR 文本（MinerU 失败/降级）时拒绝，禁止空文档喂 LLM。"""
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseResult

    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="vision",
                confidence="low",
                # markdown 为空：MinerU 失败降级到纯 vision
                vision_image=b"\x89PNG\r\n\x1a\nimage",
                vision_mime="image/png",
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)

    with pytest.raises(ConvertError) as exc_info:
        asyncio.run(
            TuoshuSkill().run(
                file_bytes=b"\x89PNG\r\n\x1a\nimage", filename="order.png"
            )
        )
    assert exc_info.value.code == "vision_disabled_no_ocr"


def test_scan_pdf_vision_bytes_budget(monkeypatch):
    """扫描 PDF 渲染出的 PNG 总字节超过 vision 上限时报错，而不是超限直传。"""
    import app.skills.tuoshu.skill as skill_module

    # 启用 LLM 视觉能力，验证扫描 PDF 转 vision 的字节预算路径
    monkeypatch.setattr(settings, "llm_vision_enabled", True)
    monkeypatch.setattr(skill_module, "convert_to_markdown", lambda *_f: "SCAN_OR_IMAGE_HINT: order.pdf")
    monkeypatch.setattr(
        skill_module,
        "render_pdf_pages",
        lambda _file_bytes, **kwargs: [b"\x00" * (settings.vision_max_image_bytes // 2 + 1)] * 2,
    )

    with pytest.raises(ConvertError) as exc_info:
        asyncio.run(TuoshuSkill().run(file_bytes=b"%PDF-1.7\n", filename="order.pdf"))
    assert exc_info.value.code == "vision_image_too_large"


def test_scan_pdf_goes_to_mineru_when_vision_disabled(monkeypatch):
    """默认无视觉模型：扫描 PDF 交给 MinerU OCR 解析，而不是直接报错。"""
    import app.skills.tuoshu.skill as skill_module

    captured: dict = {}
    monkeypatch.setattr(skill_module, "convert_to_markdown", lambda *_f: "SCAN_OR_IMAGE_HINT: order.pdf")
    monkeypatch.setattr(
        skill_module.mineru,
        "parse_document",
        lambda *_args, **_kwargs: type(
            "Scanned", (), {"markdown": "提单号：KMTCSHAP950393\n托运人：某托运人公司"}
        )(),
    )

    async def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "achat_json", fake_chat_json)

    response = asyncio.run(TuoshuSkill().run(file_bytes=b"%PDF-1.7\n", filename="order.pdf"))

    user_content = captured["messages"][-1]["content"]
    # MinerU OCR 文本走纯文本通道，不再携带图片
    assert isinstance(user_content, str)
    assert "提单号：KMTCSHAP950393" in user_content
    # 扫描件无独立文本层可交叉核验，必须人工复核
    issues = response["result"]["review_issues"]
    assert any(
        issue["code"] == "scanned_pdf_ocr_unverified" and issue["blocking"]
        for issue in issues
    )
    assert response["result"]["ready_for_order"] is False


def test_scan_pdf_rejected_when_mineru_fails_and_vision_disabled(monkeypatch):
    """无视觉模型且 MinerU 也失败时，扫描 PDF 才报错（明确归因 MinerU）。"""
    import app.skills.tuoshu.skill as skill_module

    monkeypatch.setattr(skill_module, "convert_to_markdown", lambda *_f: "SCAN_OR_IMAGE_HINT: order.pdf")
    monkeypatch.setattr(
        skill_module.mineru,
        "parse_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("mineru down")),
    )

    with pytest.raises(ConvertError) as exc_info:
        asyncio.run(TuoshuSkill().run(file_bytes=b"%PDF-1.7\n", filename="order.pdf"))
    assert exc_info.value.code == "vision_disabled_no_ocr"
    assert "MinerU" in exc_info.value.message


def test_auth_middleware_enforces_api_key(monkeypatch):
    """配置 API_KEY 后：未授权 401、正确凭证放行、豁免路径不校验。"""
    monkeypatch.setattr(settings, "api_key", "test-secret-key")
    try:
        # 未携带凭证 → 401
        resp = client.get("/healthz")  # 豁免路径
        assert resp.status_code == 200
        resp = client.get("/api/logs")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"
        # 错误凭证 → 401
        resp = client.get("/api/logs", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401
        # 正确凭证（Bearer）→ 放行
        resp = client.get("/api/logs", headers={"Authorization": "Bearer test-secret-key"})
        assert resp.status_code == 200
        # 正确凭证（X-API-Key）→ 放行
        resp = client.get("/api/logs", headers={"X-API-Key": "test-secret-key"})
        assert resp.status_code == 200
    finally:
        monkeypatch.setattr(settings, "api_key", "")


def test_payload_too_large_rejected_at_transport_layer(monkeypatch):
    """JSON 请求体 Content-Length 超限直接在传输层拦截，返回 413 且留有审计记录。"""
    import app.access_log as access_log

    monkeypatch.setattr(settings, "api_max_upload_bytes", 16)
    resp = client.post(
        "/orders",
        json={"content": "x" * 100, "roomId": "room-1"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    # 声明式 413 同样必须落审计（修复前该路径无审计记录）
    entries = access_log.query(limit=1, path="/orders", status=413)
    assert entries["total"] >= 1
    assert entries["items"][0]["error_code"] == "payload_too_large"


def test_multipart_within_batch_limit_reaches_handler(monkeypatch):
    """multipart 整包超过单文件限制但不超过批量上限时，传输层放行，
    由 handler 逐文件校验（400 file_too_large 而非 413）。"""
    monkeypatch.setattr(settings, "api_max_upload_bytes", 200)
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("big.bin", b"x" * 500, "application/octet-stream")},
    )
    # multipart 整包（≈700B）< 200×10=2000，不应被传输层 413 误伤
    assert resp.status_code == 400
    assert resp.json()["code"] == "file_too_large"


def test_server_busy_returns_503(monkeypatch):
    """在途任务满载且排队超时时返回 503 server_busy（而非 500）。"""
    import asyncio

    import app.main as main

    async def _never_acquire():
        await asyncio.sleep(3600)

    monkeypatch.setattr(main._inflight_semaphore, "acquire", _never_acquire)
    monkeypatch.setattr(settings, "skill_queue_wait_seconds", 0.05)
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "server_busy"


def test_payload_too_large_chunked_rejected_at_transport_layer(monkeypatch):
    """无 Content-Length（chunked 编码）的 JSON 请求体超限同样返回 413 并留有审计记录。"""
    import app.access_log as access_log

    monkeypatch.setattr(settings, "api_max_upload_bytes", 16)

    def _chunked_body():
        yield b'{"content": "'
        yield b"x" * 100
        yield b'", "roomId": "room-1"}'

    resp = client.post(
        "/orders",
        content=_chunked_body(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    # 审计记录必须存在且状态为 413（修复前该场景无审计记录且返回 500）
    entries = access_log.query(limit=1, path="/orders", status=413)
    assert entries["total"] >= 1
    last = entries["items"][0]
    assert last["error_code"] == "payload_too_large"


def test_skill_uses_deterministic_route_when_llm_misclassifies_doc_type(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    markdown = "门点装箱通知\n车队将于 2019-09-12 14:00 以前到门点装柜"
    monkeypatch.setattr(
        skill_module,
        "convert_to_markdown",
        lambda _file_bytes, _filename: markdown,
    )

    async def fake_achat_json(_messages, **_kwargs):
        return (
            {
                "doc_type": "PACKING_NOTICE",
                "shipper_company": "某托运人公司",
                "factory": {"name": "某门点"},
                "source": {},
            },
            {"model": "fake", "usage": None},
        )

    monkeypatch.setattr(skill_module, "achat_json", fake_achat_json)

    response = asyncio.run(TuoshuSkill().run(file_bytes=b"fake-docx", filename="order.docx"))

    assert response["result"]["doc_type"] == "TRUCKING_ORDER"


def test_chat_text_contains_every_container():
    text = format_to_chat_text(
        {
            "source": {"file": "test.docx", "doc_format": "docx"},
            "containers": [
                {"type": "40HC", "container_no": "CONT001"},
                {"type": "20GP", "container_no": "CONT002"},
            ]
        }
    )

    assert "柜 1:" in text
    assert "柜 2:" in text
    assert "CONT001" in text
    assert "CONT002" in text


def test_chat_text_only_uses_present_json_content():
    text = format_to_chat_text(
        {
            "doc_type": "TRANSPORT_ORDER",
            "internal_ref": "WXHYC22010107",
            "vessel": "RESURGENCE",
            "voyage": None,
            "containers": [{"type": "40HC", "qty": 3}],
            "raw_text_snippet": "不应重新展示的原文",
            "source": {"file": "test.docx", "doc_format": "docx"},
        }
    )

    assert text == "我司业务编号: WXHYC22010107\n船名: RESURGENCE\n箱型: 40HC\n箱量: 3"


def test_chat_text_surfaces_blocking_review_issues():
    text = format_to_chat_text(
        {
            "source": {"file": "test.docx", "doc_format": "docx"},
            "review_issues": [
                {
                    "code": "conflicting_container_data",
                    "field": "containers[2]",
                    "message": "同一柜存在两组数据，需人工裁决",
                    "source_values": ["2150/5375.0/64.518", "2149/5372.5/64.487"],
                    "blocking": True,
                }
            ]
        }
    )

    assert "下单校验: 需人工复核" in text
    assert "2149/5372.5/64.487" in text


def test_display_renderers_reject_incomplete_json():
    with pytest.raises(ValidationError):
        format_to_chat_text({"carrier": "HLC"})
    with pytest.raises(ValidationError):
        to_chinese({"carrier": "HLC"})


def test_display_renderers_label_carrier_as_raw_shipping_company_value():
    data = {
        "carrier": "EMC CPS",
        "source": {"file": "test.docx", "doc_format": "docx"},
    }

    assert "船公司: EMC CPS（接口原始值）" in format_to_chat_text(data)
    assert to_chinese(data)["船公司"] == "EMC CPS（接口原始值）"
