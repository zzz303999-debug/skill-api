"""Pinned, opt-in LLM regression tests for the tuoshu prompt."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.skills.tuoshu.deterministic_mapper import map_template
from app.skills.tuoshu.finalize import finalize_extraction
from app.skills.tuoshu.normalizer import normalize_llm_output
from app.skills.tuoshu.prompt import detect_prompt_route
from app.skills.tuoshu.schema import TuoshuOutput

ROOT = Path(__file__).parents[3]
MANIFEST = Path(__file__).parents[2] / "golden" / "tuoshu_cases.json"
PENDING_MANIFEST = Path(__file__).parents[2] / "golden" / "tuoshu_pending_cases.json"
CASES = json.loads(MANIFEST.read_text(encoding="utf-8"))
PENDING_CASES = json.loads(PENDING_MANIFEST.read_text(encoding="utf-8"))
RUN_LLM_GOLDEN = os.getenv("RUN_LLM_GOLDEN") == "1"
RUN_MINERU_GOLDEN = os.getenv("RUN_MINERU_GOLDEN") == "1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_path(case: dict[str, str], field: str) -> Path:
    return ROOT / case[field]


def _canonical_expected(case: dict[str, str], markdown: str) -> dict[str, Any]:
    expected = json.loads(_case_path(case, "expected").read_text(encoding="utf-8"))
    expected = finalize_extraction(
        normalize_llm_output(expected),
        source_text=markdown,
        template_hint=case["expected_route"]["template_hint"],
    )
    source = expected.setdefault("source", {})
    source.setdefault("file", Path(case["expected"]).with_suffix("").name)
    source.setdefault("doc_format", Path(source["file"]).suffix.lstrip("."))
    return TuoshuOutput.model_validate(expected).model_dump()


def _field_diffs(expected: Any, actual: Any, path: str = "$") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        diffs: list[str] = []
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{path}.{key}"
            if key not in expected:
                diffs.append(f"{child}: unexpected {actual[key]!r}")
            elif key not in actual:
                diffs.append(f"{child}: missing, expected {expected[key]!r}")
            else:
                diffs.extend(_field_diffs(expected[key], actual[key], child))
        return diffs
    if isinstance(expected, list) and isinstance(actual, list):
        diffs = []
        if len(expected) != len(actual):
            diffs.append(f"{path}: expected {len(expected)} items, got {len(actual)}")
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=False)):
            diffs.extend(_field_diffs(expected_item, actual_item, f"{path}[{index}]"))
        return diffs
    return [] if expected == actual else [f"{path}: expected {expected!r}, got {actual!r}"]


def _text_diff(expected: str, actual: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="pinned parsed output",
            tofile="current parsed output",
        )
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_golden_fixture_integrity(case):
    assert case.get("human_verified") is True, "golden expected JSON must be human-confirmed"
    assert set(case["expected_route"]) == {
        "parser",
        "template_hint",
        "deterministic_mapper",
    }
    assert _sha256(_case_path(case, "document")) == case["document_sha256"]
    assert _sha256(_case_path(case, "parsed")) == case["parsed_sha256"]
    assert _sha256(_case_path(case, "expected")) == case["expected_sha256"]


@pytest.mark.parametrize("case", PENDING_CASES, ids=lambda case: case["name"])
def test_pending_golden_assets_are_pinned_but_not_accepted(case):
    assert case["human_verified"] is False
    assert case["name"] not in {accepted["name"] for accepted in CASES}
    assert _sha256(_case_path(case, "document")) == case["document_sha256"]
    assert _sha256(_case_path(case, "parsed")) == case["parsed_sha256"]
    assert _sha256(_case_path(case, "expected")) == case["expected_sha256"]


@pytest.mark.parametrize("case", PENDING_CASES, ids=lambda case: case["name"])
def test_pending_expected_candidate_is_schema_valid(case):
    markdown = _case_path(case, "parsed").read_text(encoding="utf-8")
    candidate = _canonical_expected(case, markdown)

    assert candidate["source"]["file"] == _case_path(case, "document").name
    assert candidate["ready_for_order"] is False


@pytest.mark.parametrize(
    "case", [case for case in CASES if case["parser_mode"] == "local"], ids=lambda case: case["name"]
)
@pytest.mark.asyncio
async def test_local_parser_golden_regression(case, monkeypatch):
    from app.core.config import settings
    from app.core.doc_convert import convert_to_markdown_async

    monkeypatch.setattr(settings, "mineru_enabled", False)
    document = _case_path(case, "document")
    expected = _case_path(case, "parsed").read_text(encoding="utf-8")
    converted = await convert_to_markdown_async(document.read_bytes(), document.name)
    actual = str(converted)

    assert actual == expected, "parser-level golden diff:\n" + _text_diff(expected, actual)


@pytest.mark.mineru_golden
@pytest.mark.skipif(not RUN_MINERU_GOLDEN, reason="set RUN_MINERU_GOLDEN=1 to call MinerU")
@pytest.mark.parametrize(
    "case", [case for case in CASES if case["parser_mode"] == "mineru"], ids=lambda case: case["name"]
)
def test_mineru_parser_golden_regression(case):
    from app.mineru.client import parse_document_async

    document = _case_path(case, "document")
    expected = _case_path(case, "parsed").read_text(encoding="utf-8")
    parsed = asyncio.run(
        parse_document_async(document.read_bytes(), document.name, mime_type="application/pdf")
    )
    actual = parsed.markdown

    assert actual == expected, "MinerU parser-level golden diff:\n" + _text_diff(expected, actual)


@pytest.mark.mineru_golden
@pytest.mark.skipif(not RUN_MINERU_GOLDEN, reason="set RUN_MINERU_GOLDEN=1 to call MinerU")
@pytest.mark.parametrize(
    "case",
    [case for case in CASES if case["parser_mode"] == "mineru_image"],
    ids=lambda case: case["name"],
)
def test_image_mineru_parser_regression(case):
    from app.mineru.client import parse_document_async

    document = _case_path(case, "document")
    expected = _case_path(case, "parsed").read_text(encoding="utf-8").strip()
    parsed = asyncio.run(
        parse_document_async(document.read_bytes(), document.name, mime_type="image/jpeg")
    )
    actual = parsed.markdown

    assert actual == expected, "MinerU image parser-level diff:\n" + _text_diff(expected, actual)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_golden_expected_json_is_schema_valid(case):
    markdown = _case_path(case, "parsed").read_text(encoding="utf-8")
    expected = _canonical_expected(case, markdown)

    assert expected["source"]["file"] == _case_path(case, "document").name
    assert expected["ready_for_order"] is not None


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_expected_template_route_is_pinned(case):
    markdown = _case_path(case, "parsed").read_text(encoding="utf-8")
    mapper = map_template(markdown)
    prompt_route = detect_prompt_route(f"{_case_path(case, 'document').name}\n{markdown}")
    expected_route = case["expected_route"]

    assert mapper.matched is expected_route["deterministic_mapper"]
    actual_hint = mapper.template_id or prompt_route.template_hint
    assert actual_hint == expected_route["template_hint"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
@pytest.mark.asyncio
async def test_quality_router_matches_expected_parser(case, monkeypatch):
    from app.core import doc_convert as convert_service
    from app.core.config import settings
    from app.mineru.client import MinerUParseResult

    document = _case_path(case, "document")
    monkeypatch.setattr(settings, "mineru_enabled", True)
    if case["parser_mode"] == "mineru_image":
        markdown = _case_path(case, "parsed").read_text(encoding="utf-8")

        async def fake_mineru(*_args, **_kwargs):
            return MinerUParseResult(markdown=markdown, table_count=2)

        monkeypatch.setattr(convert_service.mineru, "parse_document_async", fake_mineru)
        result = await convert_service.convert_image_to_parse_result_async(
            document.read_bytes(), document.name
        )
        parser = result.parser
    else:
        converted = await convert_service.convert_to_markdown_async(
            document.read_bytes(), document.name
        )
        parser = converted.parser

    assert parser == case["expected_route"]["parser"]


def test_bsse_incident_acceptance_baseline():
    case = next(case for case in CASES if case["name"] == "bsse2105280058")
    markdown = _case_path(case, "parsed").read_text(encoding="utf-8")
    result = _canonical_expected(case, markdown)

    assert result["doc_date"] == "2021-05-28"
    assert result["recipient"] == "上海运嘉货运代理有限公司"
    assert result["shipper_agent"] == "上海秉晟国际物流有限公司"
    assert result["shipper_company"] == "上海秉晟国际物流有限公司"
    assert result["order_mapping"]["c_title"] == "上海秉晟国际物流有限公司"
    assert result["loading_time"] == "2021-06-01T08:00:00"
    assert result["containers"][0]["seal_no"] is None
    assert result["containers"][0]["packages"] is None
    assert result["containers"][0]["gross_weight_kg"] is None
    assert result["containers"][0]["volume_cbm"] is None
    issue_codes = {issue["code"] for issue in result["review_issues"]}
    assert {
        "missing_container_measurements",
        "carrier_by_vessel",
    } <= issue_codes
    assert "missing_shipper_company" not in issue_codes


def test_kflse220216031_acceptance_baseline():
    case = next(case for case in CASES if case["name"] == "kflse220216031")
    markdown = _case_path(case, "parsed").read_text(encoding="utf-8")
    result = _canonical_expected(case, markdown)

    assert result["doc_date"] == "2022-02-16"
    assert result["recipient"] == "上海硕豪物流有限公司"
    assert result["shipper_agent"] == "上海凯福国际物流有限公司"
    assert result["shipper_company"] is None
    assert result["sender_contact"] == "袁丹"
    assert result["pod"] == "COLUMBUS, OH"
    assert result["loading_time"] == "2022-02-17T08:00:00"
    assert result["containers"][0]["type"] == "40HQ"
    assert result["containers"][0]["packages"] is None
    assert result["containers"][0]["packages_unit"] == "CTNS"
    assert result["containers"][0]["gross_weight_kg"] == 19000.0
    assert result["containers"][0]["volume_cbm"] is None
    assert result["containers"][0]["seal_no"] is None
    assert result["factory"]["name"] == "江西杰盛医疗制品有限公司"

    issues = result["review_issues"]
    issue_codes = [issue["code"] for issue in issues]
    assert issue_codes == [
        "missing_container_measurements",
        "carrier_by_mbl",
        "notice_remark_restored",
    ]
    assert len(issue_codes) == len(set(issue_codes))
    # 除 notice_remark_restored（内容已恢复，仅提醒确认）外均阻断下单
    blocking_issues = [issue for issue in issues if issue["code"] != "notice_remark_restored"]
    assert all(issue["blocking"] is True for issue in blocking_issues)
    restored = next(issue for issue in issues if issue["code"] == "notice_remark_restored")
    assert restored["blocking"] is False
    assert "unstructured_review_issue" not in issue_codes


def test_weishi_image_acceptance_baseline():
    case = next(case for case in CASES if case["name"] == "weishi_A26042709390557851")
    markdown = _case_path(case, "parsed").read_text(encoding="utf-8")
    result = _canonical_expected(case, markdown)

    assert result["mbl_no"] == "TEST000011"
    assert result["shipper_agent"] == "上海威世国际货物运输代理有限公司"
    assert result["containers"][0]["type"] == "40HQ"
    assert result["remark"] == "数据准确，箱单填好过去；1750"
    assert result["order_mapping"]["c_note"] == "数据准确，箱单填好过去；1750"
    assert "作业资水" not in result["order_mapping"]["c_note"]


def test_weishi_expected_combines_observed_0615_and_0633_fields():
    observed_dir = Path(__file__).parents[2] / "golden/tuoshu/observed"
    version_0615 = json.loads(
        (observed_dir / "A26042709390557851-0615.partial.json").read_text(encoding="utf-8")
    )
    version_0633 = json.loads(
        (observed_dir / "A26042709390557851-0633.partial.json").read_text(encoding="utf-8")
    )
    expected = json.loads(
        (Path(__file__).parents[2] / "golden/tuoshu/expected/A26042709390557851.json").read_text(
            encoding="utf-8"
        )
    )

    good_0615 = version_0615["known_good_fields"]
    good_0633 = version_0633["known_good_fields"]
    assert expected["mbl_no"] == good_0615["mbl_no"]
    assert expected["shipper_agent"] == good_0615["shipper_agent"]
    assert expected["remark"] == good_0615["remark"]
    assert expected["order_mapping"]["c_note"] == good_0615["order_mapping.c_note"]
    assert expected["containers"][0]["type"] == good_0633["containers[0].type"]
    assert (
        version_0633["known_bad_fields"]["order_mapping.c_note"]
        != expected["order_mapping"]["c_note"]
    )


@pytest.mark.golden
@pytest.mark.skipif(not RUN_LLM_GOLDEN, reason="set RUN_LLM_GOLDEN=1 to call the LLM gateway")
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_tuoshu_llm_golden_regression(case, monkeypatch):
    import app.skills.tuoshu.skill as skill_module
    from app.mineru.schema import ParsedPage, ParseResult

    markdown = _case_path(case, "parsed").read_text(encoding="utf-8")
    expected = _canonical_expected(case, markdown)
    filename = expected["source"]["file"]
    if case["parser_mode"] == "mineru_image":
        async def _fake_convert_image(*_args):
            return ParseResult(
                input_format="jpeg",
                pages=[
                    ParsedPage(
                        page_number=1,
                        parser="mineru",
                        markdown=markdown,
                        vision_image=_case_path(case, "document").read_bytes(),
                    )
                ],
            )

        monkeypatch.setattr(
            skill_module,
            "convert_image_to_parse_result_async",
            _fake_convert_image,
        )
    else:
        async def _fake_convert_to_markdown(*_args):
            return markdown

        monkeypatch.setattr(skill_module, "convert_to_markdown_async", _fake_convert_to_markdown)

    response = asyncio.run(
        skill_module.TuoshuSkill().run(
            file_bytes=_case_path(case, "document").read_bytes(),
            filename=filename,
        )
    )
    actual = response["result"]
    expected = deepcopy(expected)
    actual = deepcopy(actual)
    expected["source"]["extracted_at"] = None
    actual["source"]["extracted_at"] = None
    diffs = _field_diffs(expected, actual)

    assert not diffs, "field-level golden diff:\n" + "\n".join(diffs)
    usage = response["meta"].get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    if prompt_tokens is not None:
        # Image cases intentionally carry the original image as an independent
        # check against OCR hallucinations.  Keep a separate ceiling so the
        # extra safety evidence is budgeted rather than silently removed.
        max_prompt_tokens = 180_000 if case["parser_mode"] == "mineru_image" else 75_000
        assert prompt_tokens <= max_prompt_tokens
