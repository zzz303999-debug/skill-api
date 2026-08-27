"""路由测试：POST /orders/bill/import（TestClient，预览 + create 模式）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.orders.bill.client as client_module
import app.orders.bill.service as service_module
from app.config import settings
from app.main import app
from helpers import REAL_ORDER_COUNT, REAL_TOTAL_ROWS, REAL_XLS, FakeResponse

AUTH_HEADERS = {"X-API-Key": "test-secret-key"}


# AddWork 均成功（create 模式下游 mock；sk 由调用方请求头透传）
def _ok_chain_post(url, **_kwargs):
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
            body = r.json()
            # 统一响应外壳（code/msg/data，对齐 TMS 通道口径）
            assert set(body) == {"code", "msg", "data"}
            assert body["code"] == "200"
            assert body["msg"] == "请求成功"
            data = body["data"]
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
            body = r.json()
            assert body["code"] == "200" and body["msg"] == "请求成功"
            assert body["data"]["summary"] is None
            assert body["data"]["upstream"] is None
            assert all(o["create_result"] is None for o in body["data"]["orders"])


@pytest.mark.skipif(
    not REAL_XLS.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
class TestCreateMode:
    # create 模式请求头：带调用方登录 TMS 后的 sk（缺 sk → 400，见缺头用例）
    CREATE_HEADERS = {**AUTH_HEADERS, "sk": "sk-1"}

    def test_create_order_true_creates_all(self, monkeypatch):
        """create_order=true → 200：逐单 create_result + summary（每单一 AddWork；
        建档调用在端点已配后激活，不计入下单计数）。"""
        calls = {"addwork": 0}
        sk_seen: list[str | None] = []

        def fake_post(url, **_kwargs):
            if "/Car/Car" in url:  # 建档族（/Car/Car* 路径，区别于下单 /Car/WorkOut/AddWork）
                return _archive_post(url)
            sk_seen.append((_kwargs.get("headers") or {}).get("sk"))
            calls["addwork"] += 1
            return _ok_chain_post(url)

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xls",
                REAL_XLS.read_bytes(),
                data={"create_order": "true"},
                headers=self.CREATE_HEADERS,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "200" and body["msg"] == "添加成功"
        data = body["data"]
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
            "code": "200",
            "msg": "添加成功",
            "data": [{"sn": "EX26080042"}] * REAL_ORDER_COUNT,
        }
        assert all(o["create_result"]["success"] for o in data["orders"])
        assert all(o["create_result"]["sn"] == "EX26080042" for o in data["orders"])
        # 每单一次下单（建档调用不计入：端点已配后达阈值候选会建档）
        assert calls["addwork"] == REAL_ORDER_COUNT
        # sk 原样透传下游（2026-08-19 起：调用方请求头 → AddWork 请求头）
        assert sk_seen and all(s == "sk-1" for s in sk_seen)

    def test_missing_sk_400_not_per_order(self, monkeypatch):
        """create 模式缺 sk 头 → 400 bad_request（不进入解析/下单流程）。
        建档路径（端点已配后激活）同样不触发。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
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
        assert r.status_code == 400
        body = r.json()
        # 统一响应外壳（code/msg/data，v2.2 起账单录入错误场景同构）
        assert set(body) == {"code", "msg", "data"}
        assert body["code"] == "bad_request"
        assert "sk" in body["msg"]
        assert body["data"]["upstream"] == {
            "code": "400",
            "msg": "缺少 TMS token（sk 请求头），请先登录 TMS",
            "data": [],
        }
        assert calls["addwork"] == 0  # 缺 token 不进入下游

    def test_partial_failure_summary(self, monkeypatch):
        """部分单失败 → 200 + summary.failed 计数，单失败不影响其他。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
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
                headers=self.CREATE_HEADERS,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "200" and body["msg"] == "添加成功"
        data = body["data"]
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
            "code": "200",
            "msg": "添加成功",
            "data": [{"sn": "EX1"}] * (REAL_ORDER_COUNT - 1),
        }
        assert data["orders"][0]["create_result"]["success"] is False
        assert data["orders"][0]["create_result"]["error"]["details"]["upstream_code"] == "204"
        assert all(o["create_result"]["success"] for o in data["orders"][1:])

    def test_all_failed_upstream_204(self, monkeypatch):
        """全部单失败 → 200 + summary.failed 全量；upstream 返回 204 结构。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            if "/Car/Car" in url:  # 建档族：返回主键，不占 addwork 计数
                return _archive_post(url)
            calls["addwork"] += 1
            return FakeResponse({"code": "204", "msg": "添加失败"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xls",
                REAL_XLS.read_bytes(),
                data={"create_order": "true"},
                headers=self.CREATE_HEADERS,
            )
        assert r.status_code == 200
        body = r.json()
        # 全部失败：外壳 code="204" msg="添加失败"（对齐 TMS 业务码）
        assert body["code"] == "204" and body["msg"] == "添加失败"
        data = body["data"]
        assert data["summary"]["total"] == REAL_ORDER_COUNT
        assert data["summary"]["success"] == 0 and data["summary"]["failed"] == REAL_ORDER_COUNT
        # 全部失败：upstream 对齐 TMS 错误格式（code "204" + 空 data）
        assert data["upstream"] == {"code": "204", "msg": "添加失败", "data": []}
        assert all(not o["create_result"]["success"] for o in data["orders"])
        assert calls["addwork"] == REAL_ORDER_COUNT

    def test_unknown_box_type_msg_specific(self, monkeypatch):
        """箱型白名单拦截（unknown_box_type）→ 外层 msg 返回具体原因，而非笼统「添加失败」。"""
        import app.main as main_module
        from app.orders.bill import BillParseResult

        summary = {
            "total": 1,
            "success": 0,
            "failed": 1,
            "skipped": 0,
            "created": 0,
            "success_sns": [],
            "failed_details": [
                {
                    "order_num": "OOLU12345678",
                    "error_code": "unknown_box_type",
                    "error_message": "文件含非法箱型：40GOH，请联系客服",
                }
            ],
        }

        def fake_build_result(**kwargs):
            return BillParseResult(
                file=kwargs["filename"],
                total_rows=1,
                order_count=1,
                create_order=True,
                summary=summary,
                meta={},
            )

        monkeypatch.setattr(main_module, "build_result", fake_build_result)
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xlsx",
                b"x",
                data={"create_order": "true"},
                headers={**AUTH_HEADERS, "sk": "sk-1"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "204"
        assert body["msg"] == "文件含非法箱型：40GOH，请联系客服"
        assert body["data"]["summary"]["failed"] == 1


def test_unknown_box_type_zero_downstream(monkeypatch):
    """箱型不符（40GOH）create 模式 → 零下游副作用：AddWork/建档/费目自举均不调。

    2026-08-26 修正（方案 A+B）：校验被拒单排除出 pending，被拒文件不再触发
    建档（修复前实测 AddCarClient 被误调，假 token 报「请重新登录」）；
    费目自举走建档族 /Car/CarPrice，同计入 archive 计数。
    """
    from io import BytesIO

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(
        [
            "序号", "日期", "当前状态", "客户名称", "客户联系人", "客户编号", "业务类型",
            "提单号", "门点", "箱型", "联系人", "联系电话", "装卸货地点", "装卸货地址",
            "箱号", "做箱时间", "港区", "提箱堆场", "还箱堆场", "车牌号", "车队", "司机",
            "司机手机", "运费", "待时费", "预提费", "洋山费", "落/还箱费", "其它费",
            "已收/付金额", "备注", "应付备注",
        ]
    )
    ws.append(
        [
            1, "1-27", "", "测试客户", "", "TST001", "", "OOLU40GOH001", "测试门点",
            "40GOH", "", "", "测试地址", "", "TCLU4000001", "", "洋山", "", "",
            "", "", "", "", 2100, "", "", "", "", "", "", "", "",
        ]
    )
    buf = BytesIO()
    wb.save(buf)

    calls = {"addwork": 0, "archive": 0}

    def fake_post(url, **_kwargs):
        if "/Car/Car" in url:  # 建档族（CarClient/CarFactory/CarTruck/CarDriver/CarPrice）
            calls["archive"] += 1
            return _archive_post(url)
        calls["addwork"] += 1
        return _ok_chain_post(url)

    monkeypatch.setattr(client_module.httpx, "post", fake_post)
    with TestClient(app) as client:
        r = upload(
            client,
            "goh.xlsx",
            buf.getvalue(),
            data={"create_order": "true"},
            headers={**AUTH_HEADERS, "sk": "sk-1"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "204"
    assert "40GOH" in body["msg"]
    assert body["data"]["summary"]["created"] == 0
    assert body["data"]["summary"]["failed"] == 1
    # 零下游调用：AddWork 0、建档族 0（含费目自举 AddCarPrice）
    assert calls["addwork"] == 0
    assert calls["archive"] == 0


def test_error_envelope_payload_too_large_413(monkeypatch):
    """请求体超限（传输层 413）→ 统一外壳 code/msg/data（v2.2 起）。"""
    monkeypatch.setattr(settings, "api_max_upload_bytes", 1024)
    monkeypatch.setattr(settings, "api_batch_max_files", 2)
    with TestClient(app) as client:
        r = upload(client, "big.xls", b"x" * 4096)
    assert r.status_code == 413
    body = r.json()
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == "payload_too_large"
    assert body["msg"] == "请求体超过大小限制，请压缩或拆分后重试"
    assert body["data"]["max_bytes"] == 1024 * 2


def test_error_envelope_rate_limited_429(monkeypatch):
    """限流 429 → 统一外壳 code/msg/data + Retry-After（v2.2 起）。"""
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(main_module, "_rate_limit_whitelist", frozenset())
    monkeypatch.setattr(main_module._LIMITERS["heavy"], "max_requests", 1)
    monkeypatch.setattr(main_module._LIMITERS["heavy"], "_hits", {})
    with TestClient(app) as client:
        client.post("/orders/bill/import")  # 第 1 次放行（缺 file 422 也经中间件）
        r = client.post("/orders/bill/import")  # 第 2 次 429
    assert r.status_code == 429
    body = r.json()
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == "rate_limited"
    assert body["msg"] == "请求过于频繁，已被限流，请稍后重试"
    assert body["data"]["retry_after_seconds"] >= 0


def test_error_envelope_server_busy_503(monkeypatch):
    """服务繁忙 503 → 统一外壳 code/msg/data（v2.2 起）。"""
    import app.main as main_module
    from app.errors import ServiceBusyError

    def fake_build_result(**kwargs):
        raise ServiceBusyError("concurrent tasks full, queue timeout")

    monkeypatch.setattr(main_module, "build_result", fake_build_result)
    with TestClient(app) as client:
        r = upload(client, "b.xlsx", b"x")
    assert r.status_code == 503
    body = r.json()
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == "server_busy"
    assert body["msg"] == "服务繁忙（并发处理任务已满），请稍后重试"
    assert body["data"] is None


_QIYU_2019 = Path("tests/golden/bill/families/qiuyi/2019-01到2019-12上海秋怡应收对账单.xls")


@pytest.mark.skipif(
    not _QIYU_2019.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
def test_qiuyi_20hq_whole_file_rejected(monkeypatch):
    """秋怡 2019（含 20HQ，2026-08-26 用户确认非法）→ 整批拒绝、零下游。"""
    calls = {"addwork": 0, "archive": 0}

    def fake_post(url, **_kwargs):
        if "/Car/Car" in url:  # 建档族
            calls["archive"] += 1
            return _archive_post(url)
        calls["addwork"] += 1
        return _ok_chain_post(url)

    monkeypatch.setattr(client_module.httpx, "post", fake_post)
    with TestClient(app) as client:
        r = upload(
            client,
            _QIYU_2019.name,
            _QIYU_2019.read_bytes(),
            data={"create_order": "true"},
            headers={**AUTH_HEADERS, "sk": "sk-1"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "204"
    assert "20HQ" in body["msg"]
    assert body["data"]["summary"]["created"] == 0
    assert body["data"]["summary"]["failed"] == body["data"]["summary"]["total"]
    assert calls["addwork"] == 0
    assert calls["archive"] == 0


class TestFileErrors:
    def _upload(self, client, filename: str, content: bytes):
        return upload(client, filename, content, headers=AUTH_HEADERS)

    def test_all_file_errors(self):
        """空文件/坏扩展名/伪装扩展名/坏文件 → 各错误码（统一外壳 code 字段）。"""
        with TestClient(app) as client:
            r = self._upload(client, "empty.xls", b"")
            assert r.status_code == 400 and r.json()["code"] == "empty_file"
            r = self._upload(client, "a.txt", b"hello")
            assert r.status_code == 400 and r.json()["code"] == "bad_request"
            r = self._upload(client, "fake.xlsx", REAL_XLS.read_bytes())
            assert r.status_code == 400
            assert r.json()["code"] == "file_format_mismatch"
            r = self._upload(client, "bad.xlsx", b"not a zip")
            assert r.status_code == 422 and r.json()["code"] == "convert_error"

    def test_too_large(self):
        """超 20MB → 400 file_too_large（统一外壳）。"""
        with TestClient(app) as client:
            big = b"x" * (settings.api_max_upload_bytes + 1)
            r = self._upload(client, "big.xls", big)
            assert r.status_code == 400 and r.json()["code"] == "file_too_large"

    def test_too_many_rows(self, monkeypatch):
        """数据行数超单次导入上限 → 400 too_many_rows（BillRow 管线口径）。"""
        from types import SimpleNamespace

        monkeypatch.setattr(settings, "bill_import_max_rows", 2)
        monkeypatch.setattr(
            service_module,
            "parse_bill",
            lambda _path: SimpleNamespace(rows=[None] * 3, canonical_rows=None, period=None),
        )
        with TestClient(app) as client:
            r = self._upload(client, "many.xlsx", b"x")
            assert r.status_code == 400
            body = r.json()
            # 统一外壳：code 错误码、msg 中文说明、data 原 details
            assert set(body) == {"code", "msg", "data"}
            assert body["code"] == "too_many_rows"
            assert body["msg"] == "数据量过大，联系人工客服"
            assert body["data"] == {
                "total_rows": 3,
                "max_rows": 2,
                # 对齐 create 模式 upstream 结构，保证错误码可达对接方
                "upstream": {"code": "400", "msg": "数据量过大，联系人工客服", "data": []},
            }

    def test_too_many_rows_canonical(self, monkeypatch):
        """canonical（标准字段/TMS）管线行数超限同样拦截（rows 恒空时也不漏拦）。"""
        from types import SimpleNamespace

        monkeypatch.setattr(settings, "bill_import_max_rows", 2)
        monkeypatch.setattr(
            service_module,
            "parse_bill",
            lambda _path: SimpleNamespace(rows=[], canonical_rows=[None] * 3, period=None),
        )
        with TestClient(app) as client:
            r = self._upload(client, "many.xlsx", b"x")
            assert r.status_code == 400
            assert r.json()["code"] == "too_many_rows"
            assert r.json()["data"]["total_rows"] == 3

    def test_too_many_rows_create_mode_zero_side_effect(self, monkeypatch):
        """create_order=true 超限 → 400 且零副作用（不触达下游/去重注册表）。"""
        from types import SimpleNamespace

        from app.orders.bill import imported_registry

        monkeypatch.setattr(settings, "bill_import_max_rows", 2)
        monkeypatch.setattr(
            service_module,
            "parse_bill",
            lambda _path: SimpleNamespace(rows=[None] * 3, canonical_rows=None, period=None),
        )
        calls = {"addwork": 0, "lookup": 0, "register": 0}
        registry = imported_registry.get_imported_registry()

        def fake_post(url, **_kwargs):
            calls["addwork"] += 1
            return _ok_chain_post(url)

        def counting(fn, key):
            def wrapped(*a, **k):
                calls[key] += 1
                return fn(*a, **k)

            return wrapped

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        monkeypatch.setattr(registry, "lookup", counting(registry.lookup, "lookup"))
        monkeypatch.setattr(registry, "register", counting(registry.register, "register"))
        with TestClient(app) as client:
            r = upload(
                client,
                "many.xlsx",
                b"x",
                data={"create_order": "true"},
                headers={**AUTH_HEADERS, "sk": "sk-1"},
            )
        assert r.status_code == 400
        assert r.json()["code"] == "too_many_rows"
        assert calls["addwork"] == 0  # 拦截先于一切下单调用
        assert calls["lookup"] == 0 and calls["register"] == 0  # 去重注册表零触达

    def test_max_rows_boundary_ok(self, monkeypatch):
        """行数 == 上限不拦截（canonical 管线 1 行、上限 1 → 200 放行）。"""
        from helpers import build_bill_bytes

        headers = {
            "A": "序号",
            "B": "客户名称",
            "C": "门点",
            "D": "箱型箱量",
            "E": "提单号",
            "F": "箱号",
            "G": "做箱时间",
            "H": "港区",
            "I": "司机",
            "J": "应收备注",
        }
        monkeypatch.setattr(settings, "bill_import_max_rows", 1)
        with TestClient(app) as client:
            r = self._upload(
                client,
                "junyu.xlsx",
                build_bill_bytes(
                    headers,
                    [{"A": 1, "B": "客户甲", "E": "OOLU12345678", "D": "40HQ", "F": "TCLU1"}],
                ),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "200" and body["msg"] == "请求成功"
        assert body["data"]["create_order"] is False

    def test_error_structure(self):
        """错误响应统一外壳三字段结构（code/msg/data，v2.2 起）。"""
        with TestClient(app) as client:
            r = self._upload(client, "fake.xlsx", REAL_XLS.read_bytes())
            assert set(r.json()) == {"code", "msg", "data"}


class TestNanEcho:
    """下游回显含 NaN/Infinity 字面量 → 归一为 null：响应完整可序列化（不 500）。"""

    @staticmethod
    def _junyu_file() -> bytes:
        from helpers import build_bill_bytes

        headers = {
            "A": "序号",
            "B": "客户名称",
            "C": "门点",
            "D": "箱型箱量",
            "E": "提单号",
            "F": "箱号",
            "G": "做箱时间",
            "H": "港区",
            "I": "司机",
            "J": "应收备注",
        }
        return build_bill_bytes(
            headers,
            [{"A": 1, "B": "客户甲", "E": "OOLU12345678", "D": "40HQ", "F": "TCLU1"}],
        )

    def test_nan_upstream_returns_200(self, monkeypatch):
        """AddWork 回显 data[0] 含 NaN → 200 + upstream 中为 null（序列化不炸）。"""

        def fake_post(url, **_kwargs):
            if "/Car/Car" in url:  # 建档族：返回主键，不占 addwork 计数
                return _archive_post(url)
            return FakeResponse(
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX1", "fee": float("nan")}]}
            )

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with TestClient(app) as client:
            r = upload(
                client,
                "junyu.xlsx",
                self._junyu_file(),
                data={"create_order": "true"},
                headers={**AUTH_HEADERS, "sk": "sk-1"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "200" and body["msg"] == "添加成功"
        data = body["data"]
        assert data["upstream"] == {
            "code": "200",
            "msg": "添加成功",
            "data": [{"sn": "EX1", "fee": None}],
        }
        assert data["canonical_orders"][0]["create_result"]["upstream"]["fee"] is None


class TestAuth:
    def test_import_exempt_from_auth(self, monkeypatch):
        """配置 api_key 后：/orders/bill/import 鉴权豁免（内网免 key 场景），
        无凭证/错凭证均放行；其他接口（/api/logs）仍需鉴权。"""
        monkeypatch.setattr(settings, "api_key", "test-secret-key")
        with TestClient(app) as client:
            # 上传接口豁免：无凭证、错凭证、正确凭证均 200
            r = upload(client, "b.xls", REAL_XLS.read_bytes())
            assert r.status_code == 200
            r = upload(
                client,
                "b.xls",
                REAL_XLS.read_bytes(),
                headers={"X-API-Key": "wrong-key"},
            )
            assert r.status_code == 200
            r = upload(client, "b.xls", REAL_XLS.read_bytes(), headers=AUTH_HEADERS)
            assert r.status_code == 200
            # 非豁免接口仍强制鉴权
            r = client.get("/api/logs")
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "unauthorized"
            r = client.get("/api/logs", headers=AUTH_HEADERS)
            assert r.status_code == 200

    def test_import_unified_401_when_not_exempt(self, monkeypatch):
        """豁免移除后 /orders/bill/import 无凭证 → 401 统一外壳三字段。

        审查修正 2026-08-27：默认 _AUTH_FREE_PATHS 豁免该路径使统一 401 分支
        不可达零测试，此处清空豁免验证分支行为（对外开放部署移出豁免时生效）。
        """
        import app.main as main_module
        from app.errors import ERROR_CODE_DESCRIPTIONS

        monkeypatch.setattr(settings, "api_key", "test-secret-key")
        monkeypatch.setattr(main_module, "_AUTH_FREE_PATHS", frozenset())
        with TestClient(app) as client:
            r = client.post(
                "/orders/bill/import",
                files={"file": ("b.xls", REAL_XLS.read_bytes(), "application/octet-stream")},
            )
        assert r.status_code == 401
        body = r.json()
        assert set(body) == {"code", "msg", "data"}
        assert body["code"] == "unauthorized"
        assert body["msg"] == ERROR_CODE_DESCRIPTIONS["unauthorized"]
        assert body["data"] is None

    def test_missing_file_field_422(self):
        """缺 file 字段 → 422 统一外壳（code/msg/data，v2.2 起）。"""
        with TestClient(app) as client:
            r = client.post("/orders/bill/import", headers=AUTH_HEADERS)
            assert r.status_code == 422
            body = r.json()
            assert set(body) == {"code", "msg", "data"}
            assert body["code"] == "bad_request"
            assert isinstance(body["data"]["errors"], list) and body["data"]["errors"]


def test_error_envelope_trailing_slash_413(monkeypatch):
    """尾斜杠路径 /orders/bill/import/ 错误响应同样套统一外壳。

    审查修正 2026-08-27：中间件/422 处理器先于路由执行（307 重定向前），
    去尾斜杠归一前该路径会回退旧 {error:...} 结构，同接口两种结构并存。
    """
    monkeypatch.setattr(settings, "api_max_upload_bytes", 1024)
    monkeypatch.setattr(settings, "api_batch_max_files", 2)
    with TestClient(app) as client:
        r = client.post(
            "/orders/bill/import/",
            files={"file": ("big.xls", b"x" * 4096, "application/octet-stream")},
        )
    assert r.status_code == 413
    body = r.json()
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == "payload_too_large"


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
        """全部命中成功单注册表（无新建）→ 409 统一外壳（code="409" + 业务数据）。"""
        self._fake_build_result(monkeypatch, _dedup_summary(skipped=2, created=0))
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xlsx",
                b"x",
                data={"create_order": "true"},
                headers={**AUTH_HEADERS, "sk": "sk-1"},
            )
        assert r.status_code == 409
        body = r.json()
        # 统一外壳（code/msg/data，对齐 TMS 通道口径）
        assert set(body) == {"code", "msg", "data"}
        assert body["code"] == "409"
        assert body["msg"] == "账单已全部创建过"
        assert body["data"]["summary"]["skipped"] == 2
        assert body["data"]["summary"]["created"] == 0
        assert body["data"]["summary"]["success_sns"] == ["EX26080042", "EX26080042"]

    def test_mixed_skipped_and_failed_not_409(self, monkeypatch):
        """skipped（重复）与 failed（箱型拒绝等）混合且无新建 → 204 而非 409。

        v2.2 修正：409 仅当全部单均为重复上传；混合场景存在被拒单，
        外层 msg 取具体失败原因（如箱型不符文案），不误报「账单已全部创建过」。
        """
        summary = {
            "total": 3,
            "success": 0,
            "failed": 2,
            "skipped": 1,
            "created": 0,
            "success_sns": ["EX26080042"],
            "failed_details": [
                {
                    "order_num": "OOLU11111111",
                    "error_code": "unknown_box_type",
                    "error_message": "文件含非法箱型：40GOH，请联系客服",
                }
            ],
        }
        self._fake_build_result(monkeypatch, summary)
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xlsx",
                b"x",
                data={"create_order": "true"},
                headers={**AUTH_HEADERS, "sk": "sk-1"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "204"
        assert "40GOH" in body["msg"]
        assert body["data"]["summary"]["skipped"] == 1
        assert body["data"]["summary"]["failed"] == 2

    def test_partial_skipped_still_200(self, monkeypatch):
        """部分命中（有新建）→ 200，明细在 data.summary（不误报错误）。"""
        self._fake_build_result(monkeypatch, _dedup_summary(skipped=1, created=1))
        with TestClient(app) as client:
            r = upload(
                client,
                "b.xlsx",
                b"x",
                data={"create_order": "true"},
                headers={**AUTH_HEADERS, "sk": "sk-1"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "200" and body["msg"] == "添加成功"
        data = body["data"]
        assert data["summary"]["skipped"] == 1
        assert data["summary"]["created"] == 1

    def test_duplicate_upload_full_chain_409(self, monkeypatch, real_xls_bytes):
        """真实链路：首次创建并登记 → 同文件重导全部命中 → 409（下游 0 次新增）。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
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
                headers={**AUTH_HEADERS, "sk": "sk-1"},
            )
            assert first.status_code == 200
            second = upload(
                client,
                "b.xls",
                real_xls_bytes,
                data={"create_order": "true"},
                headers={**AUTH_HEADERS, "sk": "sk-1"},
            )
        assert second.status_code == 409
        body = second.json()
        assert body["code"] == "409"
        assert body["msg"] == "账单已全部创建过"
        assert body["data"]["summary"]["created"] == 0
        assert body["data"]["summary"]["skipped"] == REAL_ORDER_COUNT
        assert calls["addwork"] == REAL_ORDER_COUNT  # 重导不再调用下游
