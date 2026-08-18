"""路由测试：POST /orders/bill/import（TestClient，预览 + create 模式）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.orders.bill.client as client_module
from app.config import settings
from app.main import app
from helpers import REAL_ORDER_COUNT, REAL_TOTAL_ROWS, REAL_XLS, FakeResponse

AUTH_HEADERS = {"X-API-Key": "test-secret-key"}


# GetWebKey/login/AddWork 均成功（create 模式下游 mock）
def _ok_chain_post(url, **_kwargs):
    if "GetWebKey" in url:
        return FakeResponse({"code": 200, "msg": "操作成功", "web_key": "wk-1"})
    if "login" in url:
        return FakeResponse({"code": 200, "data": {"token": "sk-1"}, "msg": "操作成功"})
    return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080042"}]})


# 建档族 mock 响应（端点已配后 create 模式会触发建档调用；返回对应主键、
# 不消耗下单响应序列/计数——与 test_master_data.TestGoldenIntegration 同口径）。
# 建档族路径特征 /Car/Car*（CarClient/CarFactory/CarTruck/CarDriverGroup/CarPrice），
# 注意下单 AddWork 也在 s3.jxt56.com/Car/WorkOut/AddWork，不能按 /Car/ 或 host 判断
def _archive_post(url: str):
    pk = (
        "client_id"
        if "CarClient" in url
        else "factory_id"
        if "CarFactory" in url
        else "truck_id"
        if "CarTruck" in url
        else "id"  # 司机主键是 id（逆推规范 §14）
    )
    return FakeResponse({"code": "200", "msg": "添加成功", "data": {pk: "aid-mock"}})


def upload(
    client, filename: str, content: bytes, data: dict | None = None, headers: dict | None = None
):
    return client.post(
        "/orders/bill/import",
        files={"file": (filename, content, "application/octet-stream")},
        data=data or {},
        headers=headers or {},
    )


@pytest.mark.skipif(
    not REAL_XLS.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
class TestPreview:
    def test_real_xls_preview_200(self):
        """真实 .xls 上传 → 200 预览：820 单 / 1090 行 / xlrd / 预览语义。"""
        with TestClient(app) as client:
            r = upload(client, REAL_XLS.name, REAL_XLS.read_bytes(), headers=AUTH_HEADERS)
            assert r.status_code == 200
            data = r.json()
            assert set(data) == {
                "file",
                "bill_period",
                "total_rows",
                "order_count",
                "create_order",
                "orders",
                "canonical_orders",
                "summary",
                "upstream",
                "meta",
            }
            assert data["order_count"] == REAL_ORDER_COUNT
            assert data["total_rows"] == REAL_TOTAL_ROWS
            assert data["bill_period"] == "2015-01-01~2015-12-31"
            assert data["meta"]["parser"] == "xlrd"
            assert data["create_order"] is False
            assert data["summary"] is None
            assert all(o["create_result"] is None for o in data["orders"])

    def test_preview_create_result_null(self):
        """预览模式 create_result/summary/upstream 全 null（零下游调用）。"""
        with TestClient(app) as client:
            r = upload(client, "b.xls", REAL_XLS.read_bytes(), headers=AUTH_HEADERS)
            assert r.status_code == 200
            assert r.json()["summary"] is None
            assert r.json()["upstream"] is None
            assert all(o["create_result"] is None for o in r.json()["orders"])


@pytest.mark.skipif(
    not REAL_XLS.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
class TestCreateMode:
    def test_create_order_true_creates_all(self, monkeypatch):
        """create_order=true → 200：逐单 create_result + summary（每单一 AddWork；
        建档调用在端点已配后激活，不计入下单计数）。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            if "GetWebKey" in url or "login" in url:
                return _ok_chain_post(url)
            if "/Car/Car" in url:  # 建档族（/Car/Car* 路径，区别于下单 /Car/WorkOut/AddWork）
                return _archive_post(url)
            calls["addwork"] += 1
            return _ok_chain_post(url)

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xls",
                REAL_XLS.read_bytes(),
                data={"create_order": "true"},
                headers=AUTH_HEADERS,
            )
        assert r.status_code == 200
        data = r.json()
        assert data["create_order"] is True
        assert data["summary"] == {
            "total": REAL_ORDER_COUNT,
            "success": REAL_ORDER_COUNT,
            "failed": 0,
            "skipped": 0,
            "created": REAL_ORDER_COUNT,
            "success_sns": ["EX26080042"] * REAL_ORDER_COUNT,
            "failed_details": [],
        }
        assert data["upstream"] == {
            "code": 200,
            "msg": "添加成功",
            "data": [{"sn": "EX26080042"}] * REAL_ORDER_COUNT,
        }
        assert all(o["create_result"]["success"] for o in data["orders"])
        assert all(o["create_result"]["sn"] == "EX26080042" for o in data["orders"])
        # 每单一次下单（建档调用不计入：端点已配后达阈值候选会建档）
        assert calls["addwork"] == REAL_ORDER_COUNT

    def test_credential_failure_502_not_per_order(self, monkeypatch):
        """GetWebKey 失败 → 全局 502 order_upstream_error；下单 0 次（不逐单）。
        建档路径（端点已配后激活）凭证失败仅结构化进报告，不抛断。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            if "GetWebKey" in url:
                return FakeResponse({"code": 500, "msg": "凭据无效"})
            calls["addwork"] += 1
            return FakeResponse({"code": "200", "msg": "添加成功"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xls",
                REAL_XLS.read_bytes(),
                data={"create_order": "true"},
                headers=AUTH_HEADERS,
            )
        assert r.status_code == 502
        error = r.json()["error"]
        assert error["code"] == "order_upstream_error"
        assert error["description"] == "订单系统拒绝了请求或不可达，请稍后重试"
        assert error["details"]["upstream_code"] == 500
        assert calls["addwork"] == 0  # 凭证失败 → 不逐单执行

    def test_partial_failure_summary(self, monkeypatch):
        """部分单失败 → 200 + summary.failed 计数，单失败不影响其他。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            if "GetWebKey" in url:
                return FakeResponse({"code": 200, "msg": "ok", "web_key": "wk"})
            if "login" in url:
                return FakeResponse({"code": 200, "data": {"token": "sk"}, "msg": "ok"})
            if "/Car/Car" in url:  # 建档族：返回主键，不占 addwork 计数
                return _archive_post(url)
            calls["addwork"] += 1
            if calls["addwork"] == 1:
                return FakeResponse({"code": "204", "msg": "添加失败"})
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xls",
                REAL_XLS.read_bytes(),
                data={"create_order": "true"},
                headers=AUTH_HEADERS,
            )
        assert r.status_code == 200
        data = r.json()
        assert data["summary"] == {
            "total": REAL_ORDER_COUNT,
            "success": REAL_ORDER_COUNT - 1,
            "failed": 1,
            "skipped": 0,
            "created": REAL_ORDER_COUNT - 1,
            "success_sns": ["EX1"] * (REAL_ORDER_COUNT - 1),
            "failed_details": [
                {
                    "order_num": data["orders"][0]["order_num1"],
                    "error_code": "order_upstream_error",
                    "error_message": "AddWork rejected the order: 添加失败",
                }
            ],
        }
        # 部分失败：upstream 只含成功单回显（与 /orders 的 upstream 同构）
        assert data["upstream"] == {
            "code": 200,
            "msg": "添加成功",
            "data": [{"sn": "EX1"}] * (REAL_ORDER_COUNT - 1),
        }
        assert data["orders"][0]["create_result"]["success"] is False
        assert data["orders"][0]["create_result"]["error"]["details"]["upstream_code"] == "204"
        assert all(o["create_result"]["success"] for o in data["orders"][1:])


class TestFileErrors:
    def _upload(self, client, filename: str, content: bytes):
        return upload(client, filename, content, headers=AUTH_HEADERS)

    def test_all_file_errors(self):
        """空文件/坏扩展名/伪装扩展名/坏文件 → 各错误码。"""
        with TestClient(app) as client:
            r = self._upload(client, "empty.xls", b"")
            assert r.status_code == 400 and r.json()["error"]["code"] == "empty_file"
            r = self._upload(client, "a.txt", b"hello")
            assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
            r = self._upload(client, "fake.xlsx", REAL_XLS.read_bytes())
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "file_format_mismatch"
            r = self._upload(client, "bad.xlsx", b"not a zip")
            assert r.status_code == 422 and r.json()["error"]["code"] == "convert_error"

    def test_too_large(self):
        """超 20MB → 400 file_too_large。"""
        with TestClient(app) as client:
            big = b"x" * (settings.api_max_upload_bytes + 1)
            r = self._upload(client, "big.xls", big)
            assert r.status_code == 400 and r.json()["error"]["code"] == "file_too_large"

    def test_error_structure(self):
        """错误响应统一四字段结构。"""
        with TestClient(app) as client:
            r = self._upload(client, "fake.xlsx", REAL_XLS.read_bytes())
            assert set(r.json()["error"]) == {"code", "message", "description", "details"}


class TestAuth:
    def test_unauthorized(self, monkeypatch):
        """配置 api_key 后：无凭证/错凭证 401，正确凭证 200。"""
        monkeypatch.setattr(settings, "api_key", "test-secret-key")
        with TestClient(app) as client:
            r = upload(client, "b.xls", REAL_XLS.read_bytes())
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "unauthorized"
            r = upload(
                client,
                "b.xls",
                REAL_XLS.read_bytes(),
                headers={"X-API-Key": "wrong-key"},
            )
            assert r.status_code == 401
            r = upload(client, "b.xls", REAL_XLS.read_bytes(), headers=AUTH_HEADERS)
            assert r.status_code == 200

    def test_missing_file_field_422(self):
        """缺 file 字段 → FastAPI 默认 422（detail 结构）。"""
        with TestClient(app) as client:
            r = client.post("/orders/bill/import", headers=AUTH_HEADERS)
            assert r.status_code == 422
            assert "detail" in r.json()


def _dedup_summary(skipped: int, created: int) -> dict:
    """构造去重语义的 summary（success 含 skipped 单）。"""
    total = skipped + created
    return {
        "total": total,
        "success": total,
        "failed": 0,
        "skipped": skipped,
        "created": created,
        "success_sns": ["EX26080042"] * total,
        "failed_details": [],
    }


class TestDedupConflict:
    """去重 409 语义（HTTP 层）：create 模式全部命中 → 409；部分命中 → 200。"""

    @staticmethod
    def _fake_build_result(monkeypatch, summary: dict):
        import app.main as main_module
        from app.orders.bill import BillParseResult

        def fake_build_result(**kwargs):
            return BillParseResult(
                file=kwargs["filename"],
                total_rows=2,
                order_count=2,
                create_order=True,
                summary=summary,
                meta={},
            )

        monkeypatch.setattr(main_module, "build_result", fake_build_result)

    def test_all_skipped_returns_409(self, monkeypatch):
        """全部命中成功单注册表（无新建）→ 409 duplicate_bill + 已存在单号。"""
        self._fake_build_result(monkeypatch, _dedup_summary(skipped=2, created=0))
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xlsx",
                b"x",
                data={"create_order": "true"},
                headers=AUTH_HEADERS,
            )
        assert r.status_code == 409
        body = r.json()
        assert body["error"]["code"] == "duplicate_bill"
        assert body["error"]["details"]["success_sns"] == ["EX26080042", "EX26080042"]
        assert body["error"]["details"]["summary"]["skipped"] == 2
        assert body["error"]["details"]["summary"]["created"] == 0
        assert body["error"]["description"]  # 中文说明可展示

    def test_partial_skipped_still_200(self, monkeypatch):
        """部分命中（有新建）→ 200，明细在 summary（不误报错误）。"""
        self._fake_build_result(monkeypatch, _dedup_summary(skipped=1, created=1))
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xlsx",
                b"x",
                data={"create_order": "true"},
                headers=AUTH_HEADERS,
            )
        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["skipped"] == 1
        assert data["summary"]["created"] == 1

    def test_duplicate_upload_full_chain_409(self, monkeypatch, real_xls_bytes):
        """真实链路：首次创建并登记 → 同文件重导全部命中 → 409（下游 0 次新增）。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            if "GetWebKey" in url or "login" in url:
                return _ok_chain_post(url)
            if "/Car/Car" in url:  # 建档族（区别于下单 /Car/WorkOut/AddWork）
                return _archive_post(url)
            calls["addwork"] += 1
            return _ok_chain_post(url)

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with TestClient(app) as client:
            first = upload(
                client,
                "b.xls",
                real_xls_bytes,
                data={"create_order": "true"},
                headers=AUTH_HEADERS,
            )
            assert first.status_code == 200
            second = upload(
                client,
                "b.xls",
                real_xls_bytes,
                data={"create_order": "true"},
                headers=AUTH_HEADERS,
            )
        assert second.status_code == 409
        body = second.json()
        assert body["error"]["code"] == "duplicate_bill"
        assert body["error"]["details"]["summary"]["created"] == 0
        assert body["error"]["details"]["summary"]["skipped"] == REAL_ORDER_COUNT
        assert calls["addwork"] == REAL_ORDER_COUNT  # 重导不再调用下游
