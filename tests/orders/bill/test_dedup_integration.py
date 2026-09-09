"""(提单号, sk) 去重集成/e2e 测试：TestClient 全链路（mock 下游 AddWork，零网络）。

覆盖完整用户旅程（2026-08-31 起去重维度 = (提单号, sk)）：
preview 零触达 → sk-A 创建 → 同 sk 重传 409 → 异 sk 放行 → 各自 409 →
注册表落盘结构（无 sk 原文）→ 旧版全局注册表自动解封（生产误拦修正）→
同 sk 部分命中不误报 409。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import app.core.http_client as http_client_module
import app.orders.bill.submission.imported_registry as imported_registry
from app.main import app
from app.orders.bill.submission.imported_registry import owner_key
from helpers import FakeResponse, build_bill_bytes

AUTH_HEADERS = {"X-API-Key": "test-secret-key"}

_HEADERS = {
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

_ROW1 = {"A": 1, "B": "客户甲", "E": "OOLU10000001", "D": "40HQ", "F": "TCLU1"}
_ROW2 = {"A": 2, "B": "客户甲", "E": "OOLU10000002", "D": "40HQ", "F": "TCLU2"}


def _bill_file(rows: list[dict]) -> bytes:
    return build_bill_bytes(_HEADERS, rows)


def _upload(client, filename: str, content: bytes, *, create: bool = False, sk: str | None = None):
    data = {"create_order": "true"} if create else {}
    headers = {**AUTH_HEADERS, "sk": sk} if sk else AUTH_HEADERS
    return client.post(
        "/orders/bill/import",
        files={"file": (filename, content, "application/octet-stream")},
        data=data,
        headers=headers,
    )


# 建档族 mock 响应（与 test_route 同口径；主键映射见逆推规范 §14）
def _archive_post(url: str):
    pk = (
        "client_id"
        if "CarClient" in url
        else "factory_id"
        if "CarFactory" in url
        else "truck_id"
        if "CarTruck" in url
        else "id"
    )
    return FakeResponse({"code": "200", "msg": "添加成功", "data": {pk: "aid-mock"}})


def _patch_downstream(monkeypatch) -> dict:
    """mock 下游：AddWork 计数/记录 sk 并返回递增 sn；建档族返回主键（不计入下单）。"""
    calls: dict = {"addwork": 0, "sks": []}

    async def fake_post(url, **kwargs):
        if "/Car/Car" in url:  # 建档族（区别于下单 /Car/WorkOut/AddWork）
            return _archive_post(url)
        calls["addwork"] += 1
        calls["sks"].append((kwargs.get("headers") or {}).get("sk"))
        return FakeResponse(
            {
                "code": "200",
                "msg": "添加成功",
                "data": [{"sn": f"EX{26080000 + calls['addwork']}"}],
            }
        )

    monkeypatch.setattr(http_client_module, "_post_async", fake_post)
    return calls


class TestDedupSkE2E:
    """完整用户旅程：两个操作员（sk-A/sk-B）对同一账单的导入互不误拦。"""

    def test_full_journey_same_vs_different_sk(self, monkeypatch):
        calls = _patch_downstream(monkeypatch)
        file_bytes = _bill_file([_ROW1, _ROW2])
        registry = imported_registry.get_imported_registry()

        with TestClient(app) as client:
            # ① preview（无 sk）→ 200，注册表零触达（零副作用）
            r = _upload(client, "b.xlsx", file_bytes)
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == "200" and body["msg"] == "请求成功"
            assert registry.snapshot() == {}

            # ② sk-A 首次创建 → 200 添加成功，created=2
            r = _upload(client, "b.xlsx", file_bytes, create=True, sk="sk-A")
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == "200" and body["msg"] == "添加成功"
            assert body["data"]["summary"]["created"] == 2
            assert body["data"]["summary"]["skipped"] == 0
            assert calls["addwork"] == 2

            # ③ sk-A 重传同文件 → HTTP 409「账单已全部创建过」，下游零新增
            r = _upload(client, "b.xlsx", file_bytes, create=True, sk="sk-A")
            assert r.status_code == 409
            body = r.json()
            assert body["code"] == "409" and body["msg"] == "账单已全部创建过"
            assert body["data"]["summary"]["skipped"] == 2
            assert body["data"]["summary"]["created"] == 0
            assert calls["addwork"] == 2

            # ④ sk-B 同文件 → 放行照常创建（异 sk 不误拦），sk 原样透传下游
            r = _upload(client, "b.xlsx", file_bytes, create=True, sk="sk-B")
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == "200" and body["msg"] == "添加成功"
            assert body["data"]["summary"]["created"] == 2
            assert body["data"]["summary"]["skipped"] == 0
            assert calls["addwork"] == 4
            assert calls["sks"][-2:] == ["sk-B", "sk-B"]

            # ⑤ sk-B 重传 → 同样 409（各 sk 独立去重）
            r = _upload(client, "b.xlsx", file_bytes, create=True, sk="sk-B")
            assert r.status_code == 409
            assert r.json()["code"] == "409"
            assert calls["addwork"] == 4

        # ⑥ 注册表落盘：owner 维度两键；sk 原文不落盘（只存 sha256 前 16 hex）
        raw = registry.path.read_text(encoding="utf-8")
        assert "sk-A" not in raw and "sk-B" not in raw
        snapshot = registry.snapshot()
        assert len(snapshot) == 2  # OOLU10000001 / OOLU10000002
        for owners in snapshot.values():
            assert set(owners) == {owner_key("sk-A"), owner_key("sk-B")}

    def test_partial_hit_same_sk_not_409(self, monkeypatch):
        """同 sk 部分命中：先建 1 单，再传已建+新单的 2 单文件 → 200（不误报 409）。"""
        calls = _patch_downstream(monkeypatch)
        one = _bill_file([_ROW1])
        two = _bill_file([_ROW1, _ROW2])

        with TestClient(app) as client:
            r = _upload(client, "one.xlsx", one, create=True, sk="sk-A")
            assert r.status_code == 200
            assert r.json()["data"]["summary"]["created"] == 1

            r = _upload(client, "two.xlsx", two, create=True, sk="sk-A")
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == "200" and body["msg"] == "添加成功"  # 混合场景不 409
            summary = body["data"]["summary"]
            assert summary["total"] == 2
            assert summary["skipped"] == 1  # 已建单跳过
            assert summary["created"] == 1  # 新单照常创建
            assert calls["addwork"] == 2  # 首传 1 次 + 二传仅新单 1 次（已建单不调下游）

    def test_missing_bl_no_after_prior_success_not_409(self, monkeypatch):
        """缺号连坐 × 去重混合（2026-09-04 边界）：A 单已建成功，再传 A 原样 +
        缺号行 → A 保持 skipped 不动（不误报 409）、缺号行整批拒、code=204。"""
        from app.orders.bill.validation import MISSING_BL_NO_MSG

        calls = _patch_downstream(monkeypatch)
        first = _bill_file([_ROW1, _ROW2])
        missing_row = {**dict(_ROW2), "E": None}  # 缺提单号行（其余字段同 ROW2）
        second = _bill_file([_ROW1, missing_row, missing_row])

        with TestClient(app) as client:
            # 首次：两单建成
            r = _upload(client, "one.xlsx", first, create=True, sk="sk-A")
            assert r.json()["code"] == "200"
            assert r.json()["data"]["summary"]["created"] == 2
            assert calls["addwork"] == 2

            # 二传：ROW1 命中注册表 → skipped；两缺号行触发文件级连坐
            r = _upload(client, "two.xlsx", second, create=True, sk="sk-A")
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == "204"  # skipped 与 failed 混合 → 不误报 409
            assert body["msg"] == MISSING_BL_NO_MSG
            summary = body["data"]["summary"]
            assert summary["total"] == 3
            assert summary["skipped"] == 1  # ROW1 已建单不动
            assert summary["failed"] == 2  # 两缺号行
            assert summary["created"] == 0
            assert calls["addwork"] == 2  # 零新增下游调用

            # 单级：ROW1 保持 skipped=True 不被连坐覆盖；缺号行 missing_bl_no
            # （junyu 表头走 canonical 链：单在 canonical_orders，键为 bl_no）
            orders = body["data"]["canonical_orders"] or body["data"]["orders"]
            by_bl = {
                (o.get("bl_no") or o.get("order_num1")): o.get("create_result")
                for o in orders
            }
            r1 = by_bl.get("OOLU10000001") or {}
            assert r1.get("success") is True and r1.get("skipped") is True
            missing_codes = [
                (o.get("create_result") or {}).get("error", {}).get("code")
                for o in orders
                if not (o.get("bl_no") or o.get("order_num1"))
            ]
            assert missing_codes == ["missing_bl_no", "missing_bl_no"]

    def test_missing_bl_no_canonical_family_rejected(self, monkeypatch):
        """canonical 家族（junyu 表头）缺提单号行 → 文件级连坐同样生效
        （2026-09-04：此前连坐用例只覆盖 jinxin BillRow 链）。"""
        from app.orders.bill.validation import MISSING_BL_NO_MSG

        calls = _patch_downstream(monkeypatch)
        missing_row = {**dict(_ROW2), "E": None}
        file_bytes = _bill_file([_ROW1, missing_row])

        with TestClient(app) as client:
            # preview：msg 拦截提示，code 200
            r = _upload(client, "p.xlsx", file_bytes)
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == "200"
            assert body["msg"] == MISSING_BL_NO_MSG

            # create：整批拒、零下游
            r = _upload(client, "c.xlsx", file_bytes, create=True, sk="sk-A")
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == "204"
            assert body["msg"] == MISSING_BL_NO_MSG
            summary = body["data"]["summary"]
            assert summary["created"] == 0 and summary["failed"] == 2
            # canonical_orders 侧单级 code 同样 missing_bl_no（双表示一致）
            codes = {
                (o.get("create_result") or {}).get("error", {}).get("code")
                for o in body["data"]["canonical_orders"]
            }
            assert codes == {"missing_bl_no"}
            assert calls["addwork"] == 0

    def test_invalid_format_bl_no_canonical_chain_allowed(self, monkeypatch):
        """canonical（junyu）链：提单号列非空但格式非法（纯字母）→ 仅存在性
        判定放行（格式校验属下单链路职责，非连坐范围；与 jinxin 链差异为设计，
        见 test_route.test_invalid_format_bl_no_jinxin_chain_rejected）。"""
        calls = _patch_downstream(monkeypatch)
        # 与 _ROW1 同构，仅提单号列（E）填非法值 "ABC"
        bad_row = {k: v for k, v in dict(_ROW1).items()}
        bad_row["E"] = "ABC"
        file_bytes = _bill_file([bad_row])

        with TestClient(app) as client:
            r = _upload(client, "bad-bl.xlsx", file_bytes, create=True, sk="sk-A")
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == "200" and body["msg"] == "添加成功"  # 未被连坐
            summary = body["data"]["summary"]
            assert summary["created"] == 1 and summary["failed"] == 0
            assert calls["addwork"] == 1
            # 单级 bl_no 原文保留（无 missing 标记）
            cos = body["data"]["canonical_orders"]
            assert cos and cos[0]["bl_no"] == "ABC"
            assert cos[0]["missing_fields"] == []

    def test_legacy_global_registry_not_blocking(self, monkeypatch):
        """旧版全局注册表（跨人误拦根源）→ 自动迁移 legacy：任意 sk 照常创建。

        生产存量 imported_orders.json 为旧格式（顶层含 sn），部署后第一次
        加载即解封：旧记录保留可审计但不再拦截任何上传人。
        """
        _patch_downstream(monkeypatch)
        file_bytes = _bill_file([_ROW1, _ROW2])
        registry = imported_registry.get_imported_registry()
        legacy = {
            "OOLU10000001": {
                "sn": "EX-OLD1",
                "source_sha256": "x",
                "created_at": "2026-08-30T00:00:00Z",
            },
            "OOLU10000002": {
                "sn": "EX-OLD2",
                "source_sha256": "x",
                "created_at": "2026-08-30T00:00:00Z",
            },
        }
        registry.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        imported_registry.reload_registry(registry.path)  # 模拟部署重启后首次加载

        with TestClient(app) as client:
            r = _upload(client, "b.xlsx", file_bytes, create=True, sk="sk-X")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "200" and body["msg"] == "添加成功"
        assert body["data"]["summary"]["created"] == 2  # 存量记录不再拦截
        assert body["data"]["summary"]["skipped"] == 0
        # 旧纯提单号键保留 legacy 槽位（可审计）；新组合键（提单号+箱号）按 owner 维度并存
        snapshot = imported_registry.get_imported_registry().snapshot()
        assert "legacy" in snapshot["OOLU10000001"]
        assert "legacy" in snapshot["OOLU10000002"]
        assert owner_key("sk-X") in snapshot["OOLU10000001|TCLU1"]
        assert owner_key("sk-X") in snapshot["OOLU10000002|TCLU2"]
