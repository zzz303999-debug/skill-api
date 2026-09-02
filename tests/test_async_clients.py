"""异步客户端单测（Phase 2 双轨新增，零网络）：与同步版逐项对齐的语义验证。

覆盖：http_client 异步三日志/截断/异常上抛、共享 AsyncClient 懒加载单例、
publish_create_order_async / add_work_async / submit_canonical_async /
create_archives_async 的成功/拒绝/网络错误口径、parse_document_async 解析、
achat / achat_json 的 thinking 与 json_schema 降级路径。
同步路径既有测试（test_http_client.py 等）零改动。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import app.orders.http_client as http_client_mod
from app.config import settings
from app.document_parsers import mineru as mineru_mod
from app.document_parsers.mineru import MinerUError, parse_document_async
from app.llm import client as llm_client
from app.orders.bill import client as bill_client
from app.orders.bill import master_data_client as md_client
from app.orders.client import OrderUpstreamError, publish_create_order_async

pytestmark = pytest.mark.asyncio


class FakeResponse:
    """下游响应替身：status_code/text/json()/headers/content 最小接口。"""

    def __init__(
        self,
        text: str = "",
        status_code: int = 200,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
    ):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = headers or {}
        self.content = content

    def json(self, **_kwargs):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload

    @property
    def is_success(self) -> bool:
        return self.status_code < 400

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://downstream.test"),
                response=httpx.Response(self.status_code),
            )


class _FakeAsyncHttpClient:
    """AsyncClient 替身：捕获 post 参数，返回固定响应或抛固定异常。"""

    def __init__(self, response=None, error: Exception | None = None):
        self.captured: dict = {}
        self._response = response
        self._error = error

    async def post(self, url: str, **kwargs):
        self.captured = {"url": url, **kwargs}
        if self._error is not None:
            raise self._error
        return self._response


def _records(caplog, event: str) -> list[dict]:
    """按事件名取日志记录（对齐 test_http_client 同名 helper 口径）。"""
    return [
        record.__dict__ for record in caplog.records if record.getMessage() == event
    ]


# ---------- http_client 异步版 ----------


async def test_post_json_async_logs_request_and_response(caplog, monkeypatch):
    fake = _FakeAsyncHttpClient(
        response=FakeResponse(text='{"code": "200"}', status_code=200)
    )
    monkeypatch.setattr(http_client_mod, "get_async_client", lambda: fake)
    with caplog.at_level("INFO"):
        response = await http_client_mod.post_json_async(
            "https://downstream.test/api",
            {"data": {"order_num1": "B123"}},
            name="publishCreateOrder",
            timeout=30,
        )

    assert response.status_code == 200
    assert len(_records(caplog, "third_party_request")) == 1
    assert len(_records(caplog, "third_party_response")) == 1
    req = _records(caplog, "third_party_request")[0]
    assert req["endpoint"] == "publishCreateOrder"
    assert req["payload_kind"] == "json"
    # 请求参数与 httpx.AsyncClient.post 语义一致（json/timeout 透传）
    assert fake.captured["json"] == {"data": {"order_num1": "B123"}}
    assert fake.captured["timeout"] == 30


async def test_post_form_async_payload_kind_and_headers(caplog, monkeypatch):
    fake = _FakeAsyncHttpClient(response=FakeResponse(text="ok"))
    monkeypatch.setattr(http_client_mod, "get_async_client", lambda: fake)
    with caplog.at_level("INFO"):
        await http_client_mod.post_form_async(
            "https://downstream.test/form",
            {"b_order_num": "B123"},
            name="AddWork",
            headers={"sk": "sk-secret"},
            timeout=30,
        )

    req = _records(caplog, "third_party_request")[0]
    assert req["payload_kind"] == "form"
    assert "b_order_num" in req["body_preview"]
    # sk 凭据仅透传不落日志（与同步版口径一致）
    assert fake.captured["headers"]["sk"] == "sk-secret"
    assert "sk-secret" not in caplog.text


async def test_async_network_error_logged_and_re_raised(caplog, monkeypatch):
    fake = _FakeAsyncHttpClient(error=httpx.TimeoutException("timeout"))
    monkeypatch.setattr(http_client_mod, "get_async_client", lambda: fake)
    with caplog.at_level("INFO"):
        with pytest.raises(httpx.TimeoutException):
            await http_client_mod.post_json_async(
                "https://downstream.test/api", {}, name="addBill", timeout=30
            )

    errors = _records(caplog, "third_party_request_error")
    assert len(errors) == 1
    assert errors[0]["endpoint"] == "addBill"
    assert errors[0]["error_type"] == "TimeoutException"


async def test_async_long_body_truncated_in_logs(caplog, monkeypatch):
    """超长响应体日志截断（2000 字符），不撑爆日志。"""
    fake = _FakeAsyncHttpClient(
        response=FakeResponse(text='{"code": "200", "data": {"x": "' + "y" * 5000 + '"}}')
    )
    monkeypatch.setattr(http_client_mod, "get_async_client", lambda: fake)
    with caplog.at_level("INFO"):
        await http_client_mod.post_json_async(
            "https://downstream.test/api", {}, name="addBill", timeout=30
        )
    resp = _records(caplog, "third_party_response")[0]
    assert len(resp["body_preview"]) == 2000


async def test_async_non_json_payload_falls_back_to_str(caplog, monkeypatch):
    """非 JSON 可序列化 payload（bytes）→ 日志以 str 形态摘要，不炸。"""
    fake = _FakeAsyncHttpClient(response=FakeResponse(text="ok"))
    monkeypatch.setattr(http_client_mod, "get_async_client", lambda: fake)
    with caplog.at_level("INFO"):
        await http_client_mod.post_form_async(
            "https://downstream.test/form",
            b"raw-bytes-payload",
            name="AddWork",
            timeout=30,
        )
    req = _records(caplog, "third_party_request")[0]
    assert "raw-bytes-payload" in req["body_preview"]


async def test_get_async_client_lazy_singleton():
    """懒加载单例：重复调用返回同一连接池实例（测试后释放，不残留全局）。"""
    client = http_client_mod.get_async_client()
    try:
        assert client is http_client_mod.get_async_client()
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()
        http_client_mod._async_client = None


# ---------- publish_create_order_async ----------


async def test_publish_create_order_async_success(caplog, monkeypatch):
    async def fake_post(url, payload=None, **kwargs):
        return FakeResponse(
            text='{"code": "200", "msg": "ok"}',
            payload={"code": "200", "msg": "ok", "data": []},
        )

    monkeypatch.setattr("app.orders.client.post_json_async", fake_post)
    with caplog.at_level("INFO"):
        result = await publish_create_order_async(
            {"order_num1": "B123"}, room_id="r1", user_id="u1"
        )
    assert result["code"] == "200"


async def test_publish_create_order_async_rejected(monkeypatch):
    async def fake_post(url, payload=None, **kwargs):
        return FakeResponse(text='{"code": 204}', payload={"code": 204, "msg": "no"})

    monkeypatch.setattr("app.orders.client.post_json_async", fake_post)
    with pytest.raises(OrderUpstreamError) as exc_info:
        await publish_create_order_async({}, room_id="r1", user_id="u1")
    assert exc_info.value.details["upstream_code"] == 204


async def test_publish_create_order_async_network_error(monkeypatch):
    async def fake_post(url, payload=None, **kwargs):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr("app.orders.client.post_json_async", fake_post)
    with pytest.raises(OrderUpstreamError) as exc_info:
        await publish_create_order_async({}, room_id="r1", user_id="u1")
    assert exc_info.value.details["error_type"] == "TimeoutException"


# ---------- add_work_async / submit_canonical_async ----------


async def test_add_work_async_success_with_sn(monkeypatch):
    async def fake_post(url, data=None, **kwargs):
        assert kwargs["headers"]["sk"] == "sk-token"
        return FakeResponse(
            text='{"code": "200", "data": [{"sn": "EX26080042"}]}',
            payload={"code": "200", "data": [{"sn": "EX26080042"}]},
        )

    monkeypatch.setattr(bill_client, "post_form_async", fake_post)
    result = await bill_client.add_work_async("sk-token", {"b_order_num": "B123"})
    assert result == {
        "success": True,
        "sn": "EX26080042",
        "error": None,
        "upstream": {"sn": "EX26080042"},
    }


async def test_add_work_async_network_error_returns_error_result(monkeypatch):
    async def fake_post(url, data=None, **kwargs):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(bill_client, "post_form_async", fake_post)
    result = await bill_client.add_work_async("sk-token", {})
    # 与同步版一致：网络错误 → 按单 error 结构（不抛断整批）
    assert result["success"] is False
    assert result["sn"] is None
    assert "AddWork network error" in result["error"]["message"]


async def test_submit_canonical_async_success(monkeypatch):
    class _Order:
        bl_no = "B123"

    async def fake_post(url, data=None, **kwargs):
        return FakeResponse(
            text='{"code": "200", "data": [{"sn": "EX1", "o_id": 9}]}',
            payload={"code": "200", "data": [{"sn": "EX1", "o_id": 9}]},
        )

    monkeypatch.setattr(bill_client, "post_form_async", fake_post)
    # payload 构造属 CPU 段（真实表单字段归 app.orders.bill.payload 单元测试），
    # 此处只验网络段与响应判定：替换为最小 form + 无 warnings
    monkeypatch.setattr(
        "app.orders.bill.payload.build_order_payload",
        lambda order: ({"a": "{}"}, []),
    )
    result = await bill_client.submit_canonical_async("sk-token", _Order())
    assert result["success"] is True
    assert result["sn"] == "EX1"
    assert result["o_id"] == 9


async def test_submit_manifest_async_success(monkeypatch):
    from app.orders.manifest import client as manifest_client

    async def fake_post(url, payload=None, **kwargs):
        assert kwargs["headers"]["sk"] == "sk-token"
        return FakeResponse(
            text='{"code": 200, "msg": "成功", "data": null}',
            payload={"code": 200, "msg": "成功", "data": None},
        )

    monkeypatch.setattr(manifest_client, "post_json_async", fake_post)
    result = await manifest_client.submit_manifest_async({"bId": 1}, "sk-token")
    # code 数字 200 即成功（data null 时订单已落库，bId 缺席 sn 记空串）
    assert result["success"] is True
    assert result["sn"] == ""


async def test_submit_manifest_async_network_error(monkeypatch):
    from app.orders.manifest import client as manifest_client

    async def fake_post(url, payload=None, **kwargs):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(manifest_client, "post_json_async", fake_post)
    result = await manifest_client.submit_manifest_async({}, "sk-token")
    assert result["success"] is False
    assert "addBill network error" in result["error"]["message"]


# ---------- create_archives_async ----------


async def test_create_archives_async_success(monkeypatch):
    async def fake_post(url, data=None, **kwargs):
        return FakeResponse(
            text='{"code": "200", "data": {"client_id": "C1"}}',
            payload={"code": "200", "data": {"client_id": "C1"}},
        )

    monkeypatch.setattr(md_client, "post_form_async", fake_post)
    monkeypatch.setattr(md_client, "endpoint_for", lambda kind: "https://tms.test/add")
    results = await md_client.create_archives_async(
        {"client": {"k1": {"client_name": "x"}}}, "sk-token"
    )
    assert results["client"]["k1"]["success"] is True
    assert results["client"]["k1"]["archive_id"] == "C1"


async def test_create_archives_async_endpoint_todo(monkeypatch):
    monkeypatch.setattr(md_client, "endpoint_for", lambda kind: None)
    results = await md_client.create_archives_async(
        {"client": {"k1": {"client_name": "x"}}}, "sk-token"
    )
    entry = results["client"]["k1"]
    assert entry["success"] is False
    assert "endpoint TODO" in entry["error"]["message"]


async def test_create_archives_async_network_error(monkeypatch):
    async def fake_post(url, data=None, **kwargs):
        raise httpx.RequestError("conn reset")

    monkeypatch.setattr(md_client, "post_form_async", fake_post)
    monkeypatch.setattr(md_client, "endpoint_for", lambda kind: "https://tms.test/add")
    results = await md_client.create_archives_async(
        {"client": {"k1": {"client_name": "x"}}}, "sk-token"
    )
    entry = results["client"]["k1"]
    assert entry["success"] is False
    assert entry["error"]["code"] == "master_data_create_error"


# ---------- parse_document_async ----------


class _FakeAsyncMineruClient:
    def __init__(self, response=None, error: Exception | None = None):
        self.captured: dict = {}
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url: str, **kwargs):
        self.captured = {"url": url, **kwargs}
        if self._error is not None:
            raise self._error
        return self._response


def _mineru_response() -> FakeResponse:
    payload = {
        "content_list": [
            {"type": "text", "text": "提单号 KMTCSHAP950393 件数 12 毛重 100", "text_level": 0}
        ]
    }
    return FakeResponse(
        text='{"content_list": []}',
        payload=payload,
        headers={
            "x-mineru-version": settings.mineru_expected_version,
            "content-type": "application/json",
        },
        content=b'{"content_list": []}',
    )


async def test_parse_document_async_json_response(monkeypatch):
    monkeypatch.setattr(settings, "mineru_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_endpoint", "/file_parse")
    monkeypatch.setattr(settings, "mineru_api_key", "")
    monkeypatch.setattr(settings, "mineru_expected_version", "2.5.4")
    fake = _FakeAsyncMineruClient(response=_mineru_response())
    monkeypatch.setattr(mineru_mod.httpx, "AsyncClient", lambda **_kw: fake)

    result = await parse_document_async(b"pdf-bytes", "doc.pdf")
    assert "KMTCSHAP950393" in result.markdown
    assert result.low_confidence is False
    # multipart 上传参数透传（files/data/follow_redirects 与同步版一致）
    assert fake.captured["follow_redirects"] is True
    assert fake.captured["files"]["files"][0] == "doc.pdf"


async def test_parse_document_async_network_error(monkeypatch):
    monkeypatch.setattr(settings, "mineru_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_endpoint", "/file_parse")
    monkeypatch.setattr(settings, "mineru_api_key", "")
    monkeypatch.setattr(settings, "mineru_expected_version", "2.5.4")
    fake = _FakeAsyncMineruClient(error=httpx.TimeoutException("timeout"))
    monkeypatch.setattr(mineru_mod.httpx, "AsyncClient", lambda **_kw: fake)

    with pytest.raises(MinerUError) as exc_info:
        await parse_document_async(b"pdf-bytes", "doc.pdf")
    assert "TimeoutException" in str(exc_info.value)


# ---------- achat / achat_json ----------


class _FakeAsyncLLMCompletions:
    def __init__(self, responder):
        self._responder = responder

    async def create(self, **kwargs):
        return self._responder(kwargs)


def _install_fake_llm(monkeypatch, responder) -> list[dict]:
    """替换 AsyncOpenAI 网关；返回捕获的 kwargs 列表。

    thinking_mode 不在此设置，由调用方按需 monkeypatch（enabled=避免注入、
    disabled=验证注入与降级），避免叠加顺序覆盖。
    """
    captured: list[dict] = []

    def responder_wrapper(kwargs):
        captured.append(kwargs)
        return responder(kwargs)

    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeAsyncLLMCompletions(responder_wrapper))
    )
    monkeypatch.setattr(llm_client, "get_async_client", lambda: fake)
    return captured


def _llm_ok_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        model="test-model",
        usage=None,
    )


async def test_achat_returns_content_and_meta(monkeypatch):
    captured = _install_fake_llm(monkeypatch, lambda _kw: _llm_ok_response("hello"))
    monkeypatch.setattr("app.config.settings.llm_thinking_mode", "enabled")
    content, meta = await llm_client.achat([{"role": "user", "content": "hi"}])
    assert content == "hello"
    assert meta["model"] == "test-model"
    # thinking_mode=enabled 时不注入 thinking 参数
    assert "extra_body" not in captured[0]


async def test_achat_json_with_schema(monkeypatch):
    monkeypatch.setattr(llm_client, "_json_schema_supported", None)
    _install_fake_llm(
        monkeypatch, lambda _kw: _llm_ok_response('{"bl_no": "B123"}')
    )
    monkeypatch.setattr("app.config.settings.llm_thinking_mode", "enabled")
    data, _meta = await llm_client.achat_json(
        [{"role": "user", "content": "hi"}],
        json_schema={"type": "object", "properties": {"bl_no": {"type": "string"}}},
    )
    assert data == {"bl_no": "B123"}
    # 成功后缓存网关支持 json_schema（与同步版共享同一全局缓存）
    assert llm_client._json_schema_supported is True


async def test_achat_thinking_fallback_retry(monkeypatch):
    """thinking 参数被网关拒绝 → 去参重试一次成功；缓存置 False（双版共享）。"""
    monkeypatch.setattr(llm_client, "_thinking_disabled_supported", None)

    def responder(kwargs):
        if "thinking" in (kwargs.get("extra_body") or {}):
            raise _thinking_rejected_error()
        return _llm_ok_response("ok")

    captured = _install_fake_llm(monkeypatch, responder)
    monkeypatch.setattr("app.config.settings.llm_thinking_mode", "disabled")
    content, _meta = await llm_client.achat([{"role": "user", "content": "hi"}])
    assert content == "ok"
    assert len(captured) == 2  # 首次带 thinking 拒绝 + 去参重试
    assert "thinking" in captured[0]["extra_body"]
    assert "thinking" not in captured[1]["extra_body"]
    assert llm_client._thinking_disabled_supported is False


def _thinking_rejected_error():
    """构造网关拒绝 thinking 参数的 APIError（对齐 test_llm_client 构造模式）。"""
    from openai import BadRequestError

    response = httpx.Response(
        400,
        request=httpx.Request("POST", "http://llm.test/v1/chat/completions"),
        json={
            "error": {
                "message": "thinking is not supported",
                "type": "invalid_request_error",
            }
        },
    )
    return BadRequestError("thinking is not supported", response=response, body=None)
