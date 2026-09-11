"""舱单导入测试 fixture：合成模版 bytes、真实模版 golden、注册表隔离。"""

from __future__ import annotations

import pytest
from manifest_helpers import (
    REAL_AUTHORIZATION,
    REAL_MISSING,
    REAL_SI_V1,
    REAL_SI_V2,
    build_authorization_bytes,
    build_si_bytes,
    build_unknown_bytes,
)


@pytest.fixture(autouse=True)
def _isolate_manifest_registry(tmp_path):
    """成功单注册表隔离：每用例重建到临时目录。

    防止 create 用例（service/路由编排）写入真实 storage/imported_manifests.json
    并在用例间泄漏登记结果（去重命中会掩盖重导/并发断言）。
    """
    from app.orders.manifest.submission import imported_registry

    imported_registry.reload_registry(tmp_path / "imported_manifests.json")
    yield
    imported_registry.reload_registry(tmp_path / "imported_manifests.json")


@pytest.fixture()
def auth_bytes() -> bytes:
    return build_authorization_bytes()


@pytest.fixture()
def si_bytes() -> bytes:
    return build_si_bytes(shifted=False)


@pytest.fixture()
def si_shifted_bytes() -> bytes:
    return build_si_bytes(shifted=True)


@pytest.fixture()
def unknown_bytes() -> bytes:
    return build_unknown_bytes()


@pytest.fixture()
def real_auth_bytes() -> bytes:
    if not REAL_AUTHORIZATION.exists():
        pytest.skip(REAL_MISSING)
    return REAL_AUTHORIZATION.read_bytes()


@pytest.fixture()
def real_si_v1_bytes() -> bytes:
    if not REAL_SI_V1.exists():
        pytest.skip(REAL_MISSING)
    return REAL_SI_V1.read_bytes()


@pytest.fixture()
def real_si_v2_bytes() -> bytes:
    if not REAL_SI_V2.exists():
        pytest.skip(REAL_MISSING)
    return REAL_SI_V2.read_bytes()
