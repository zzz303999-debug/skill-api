from __future__ import annotations

from pathlib import Path

from app.skills.tuoshu.deterministic_mapper import (
    map_template,
    merge_deterministic_values,
)

BSSE_MARKDOWN = (
    Path(__file__).parent / "golden/tuoshu/parsed/bsse2105280058-mineru.md"
).read_text(encoding="utf-8")


def test_exact_bingsheng_fingerprint_maps_known_fields():
    result = map_template(BSSE_MARKDOWN)

    assert result.template_id == "bingsheng_transport"
    assert result.values["internal_ref"] == "BSSE2105280058"
    assert result.values["mbl_no"] == "1SHA044022"
    assert result.values["doc_date"] == "2021-05-28"
    assert result.values["containers"][0] == {
        "type": "40NOR",
        "qty": 1,
        "packages": 0,
        "gross_weight_kg": 0.0,
        "volume_cbm": 0.0,
    }


def test_changed_header_cannot_hit_old_template():
    changed = BSSE_MARKDOWN.replace("| 1 | 主单号 | 船名 | 航次 |", "| 1 | 提单号码 | 船名 | 航次 |")

    result = map_template(changed)

    assert result.template_id is None
    assert result.values == {}
    assert result.issues == []


def test_sentinel_failure_downgrades_and_is_blocking():
    changed = BSSE_MARKDOWN.replace("| 2 | 1SHA044022 |", "| 2 | 中文提单号 |")

    result = map_template(changed)

    assert result.template_id is None
    assert result.values == {}
    assert result.issues[0]["code"] == "template_mismatch"
    assert result.issues[0]["blocking"] is True


def test_mapper_ai_conflict_is_visible_and_mapper_value_wins():
    mapping = map_template(BSSE_MARKDOWN)
    data = {
        "mbl_no": "AI-WRONG",
        "containers": [{"type": "40HC", "po_no": "PO-1"}],
        "review_issues": [],
    }

    merged = merge_deterministic_values(data, mapping)

    assert merged["mbl_no"] == "1SHA044022"
    assert merged["containers"][0]["type"] == "40NOR"
    assert merged["containers"][0]["po_no"] == "PO-1"
    conflict_fields = {
        issue["field"]
        for issue in merged["review_issues"]
        if issue["code"] == "deterministic_ai_conflict"
    }
    assert {"mbl_no", "containers[0].type"} <= conflict_fields


def test_neizhuang_booking_maps_spaced_labels_without_mbl_carrier_fallback():
    markdown = (
        "内装箱委托书\n"
        "| 船 公 司 | 船 期 | 中转港（卸港） | 要求进港时间 |\n"
        "| --- | --- | --- | --- |\n"
        "| EMC CPS | 1月21日 | USLAX | 装好就进港 |"
    )

    result = map_template(markdown)

    assert result.template_id == "neizhuang_booking"
    assert result.values == {
        "carrier": "EMC CPS",
        "etd": "1月21日",
        "transit_port": "USLAX",
        "remark": "装好就进港",
    }


def test_neizhuang_booking_maps_mineru_html_label_value_rows():
    markdown = (
        "内装箱委托书<table>"
        "<tr><td>船 公 司</td><td>EMC CPS</td><td>船 期</td><td>1月21日</td></tr>"
        "<tr><td>中转港（卸港）</td><td>USLAX</td>"
        "<td>要求进港时间</td><td>装好就进港</td></tr></table>"
    )

    result = map_template(markdown)

    assert result.values["carrier"] == "EMC CPS"
    assert result.values["etd"] == "1月21日"
    assert result.values["transit_port"] == "USLAX"
    assert result.values["remark"] == "装好就进港"
