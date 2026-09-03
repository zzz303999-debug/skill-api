from __future__ import annotations

import pytest

from app.skills.tuoshu.prompt import detect_prompt_route


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("拖 车 委 托 书", "TRUCKING_ORDER"),
        ("暂定明早到以下地址装箱", "TRUCKING_ORDER"),
        ("配 舱 通 知 单", "BOOKING_NOTE"),
        ("BOOKING AMENDMENT", "BOOKING_NOTE"),
        ("PDL放舱说明", "BOOKING_NOTE"),
        ("装箱出运通知书", "PACKING_NOTICE"),
    ],
)
def test_real_corpus_title_variants_are_routed(text, expected):
    assert detect_prompt_route(text).doc_type == expected


def test_known_template_supplies_doc_type_when_title_is_generic():
    route = detect_prompt_route("托书 特格威 倍联业务编号 保税区")

    assert route.template_hint == "bolian_tegewei"
    assert route.doc_type == "BOOKING_NOTE"


def test_bolian_segway_header_routes_to_controlled_template():
    route = detect_prompt_route(
        "江苏倍联现代物流有限公司\n常州赛格威做箱通知\nTO：俊泰"
    )

    assert route.template_hint == "bolian_segway"
    assert route.doc_type == "PACKING_NOTICE"
