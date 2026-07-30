import export_v2_html
import export_v2_xlsx


def test_hard_excluded_candidate_is_not_labeled_as_pending_modash_work():
    candidate = {
        "handle": "hard_excluded",
        "final_pool": "Exclude",
        "modash_report": False,
    }

    assert "未补（已按硬门槛排除）" in export_v2_html._na(candidate)  # noqa: SLF001
    detail = export_v2_html._detail_panel(candidate, 16)  # noqa: SLF001
    assert "未消耗第三方报告额度" in detail
    assert "待补数" not in detail
    assert "未补（已按硬门槛排除）" in export_v2_html._brand_cell(candidate)  # noqa: SLF001
    assert (
        export_v2_xlsx._cell(candidate, "fake")  # noqa: SLF001
        == "未补（硬门槛已排除）"
    )


def test_missing_field_in_existing_report_is_labeled_as_source_unavailable():
    candidate = {
        "handle": "source_missing",
        "final_pool": "Review",
        "modash_report": True,
    }

    assert "数据源无" in export_v2_html._na(candidate)  # noqa: SLF001
    assert "数据源未识别" in export_v2_html._brand_cell(candidate)  # noqa: SLF001
    assert export_v2_xlsx._cell(candidate, "fake") == "数据源无"  # noqa: SLF001
