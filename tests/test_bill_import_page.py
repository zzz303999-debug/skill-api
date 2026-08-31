"""竞品账单上传页面（/bill-import）单元测试。

守护三层契约：
1. 页面可达性：静态页 200 + HTML + 鉴权豁免（与 _AUTH_FREE_PATHS 一致）
2. 前端-后端契约：页面内嵌 JS 消费 /orders/bill/import 统一外壳
   {code, msg, data} 的关键分支（成功解包 data.data / 204 渲染明细 /
   showError 双结构兼容 / 409 success_sns / tooltip 取 error.message），
   防止前端与后端契约漂移（v2.1/v2.2 系列修复的回归护栏）
3. JS 语法：node --check 校验内嵌脚本（node 缺失时跳过）
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"
PAGE = STATIC_DIR / "bill_import.html"
HELP_PAGE = STATIC_DIR / "bill_import_help.html"

# 页面内唯一 script 块（L528-844 内嵌，无外部 JS 依赖）
_SCRIPT_RE = re.compile(r"<script>([\s\S]*?)</script>")


def _page_text() -> str:
    return PAGE.read_text(encoding="utf-8")


def _inline_script() -> str:
    match = _SCRIPT_RE.search(_page_text())
    assert match, "页面必须包含内嵌 <script> 块"
    return match.group(1)


def test_page_200_html() -> None:
    """/bill-import → 200 + text/html + 标题。"""
    with TestClient(app) as client:
        resp = client.get("/bill-import")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "竞品账单导入" in resp.text
    assert len(resp.text) > 10_000  # 页面为完整功能页，非空壳


def test_page_auth_exempt_with_api_key(monkeypatch) -> None:
    """配置 api_key 后无凭证访问仍 200（豁免契约，与 _AUTH_FREE_PATHS 一致）。"""
    monkeypatch.setattr(settings, "api_key", "test-secret-key")
    with TestClient(app) as client:
        resp = client.get("/bill-import")
    assert resp.status_code == 200
    assert "竞品账单导入" in resp.text


def test_upload_form_elements() -> None:
    """上传表单关键元素：文件输入（三种扩展名）、上传按钮、模式切换（预览/创建）。"""
    text = _page_text()
    assert re.search(r'<input type="file" id="fileInput" accept="\.xls,\.xlsx,\.xlsm"', text)
    assert 'id="uploadBtn"' in text
    assert 'id="dropzone"' in text
    assert 'data-mode="preview"' in text and 'data-mode="create"' in text


def test_js_create_order_param() -> None:
    """create 模式才追加 create_order=true；preview 模式不追加。"""
    js = _inline_script()
    assert 'if (state.mode === "create") fd.append("create_order", "true");' in js
    assert 'fd.append("file", state.file);' in js


def test_js_success_branch_contract() -> None:
    """成功分支：code=="200" 解包 data.data；兼容旧后端无 code 键响应。"""
    js = _inline_script()
    assert 'data.code === "200" || data.code === undefined' in js
    assert "renderResult(data.code === \"200\" ? data.data : data)" in js


def test_js_204_branch_contract() -> None:
    """204 分支：仍渲染失败明细 + 标题展示 msg（可直接展示的中文原因）。"""
    js = _inline_script()
    assert 'data.code === "204"' in js
    assert "renderResult(data.data)" in js
    assert 'esc(data.msg || "添加失败")' in js


def test_js_tooltip_uses_error_message() -> None:
    """失败 tooltip 取 error.message（对象属性），而非把 error 对象直接当字符串。"""
    js = _inline_script()
    assert "(row.create_result.error && row.create_result.error.message)" in js


def test_js_show_error_dual_structure() -> None:
    """showError 双结构兼容：统一外壳（data.code/msg/data）与旧 {error: {...}}。"""
    js = _inline_script()
    assert "err.code || (data && data.code)" in js
    assert "err.description || err.message || (data && data.msg)" in js
    assert "err.details || (data && data.data)" in js


def test_js_409_success_sns_rendered() -> None:
    """409 重复导入：琥珀警示 + 已创建单号（success_sns 列表渲染）。"""
    js = _inline_script()
    assert 'code === "409" || code === "duplicate_bill"' in js
    assert "d.success_sns" in js
    assert "本次未创建新订单" in js


def test_js_syntax_valid(tmp_path) -> None:
    """内嵌 JS 语法校验（node --check）；node 缺失时跳过。"""
    if shutil.which("node") is None:
        import pytest

        pytest.skip("node 不在 PATH，跳过 JS 语法校验")
    script = _inline_script()
    js_file = tmp_path / "bill_import_inline.js"
    js_file.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        ["node", "--check", str(js_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"JS 语法错误:\n{proc.stderr}"


def test_help_page_200() -> None:
    """/bill-import-help → 200 + text/html + 手册标题。"""
    with TestClient(app) as client:
        resp = client.get("/bill-import-help")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "用户操作手册" in resp.text
