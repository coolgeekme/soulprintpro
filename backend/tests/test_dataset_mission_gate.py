"""Tests for the dataset / lead-hunt mission productivity upgrade.

Covers the Feb 18, 2026 fix where a "harvest businesses across 5 metros"
mission shipped a methodology PDF with ZERO rows. The fix adds:

  • `_is_dataset_goal` — detects list/lead-hunt/dataset intent
  • `_count_csv_data_rows` — reads the `csv_row_count` stamp on artifacts
  • DATASET_MIN_ROWS = 15 — the finish-gate threshold
  • `csv_append_rows` mission tool — incremental CSV builder
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from missions import (  # noqa: E402
    _is_dataset_goal,
    _count_csv_data_rows,
    DATASET_MIN_ROWS,
    DATASET_MAX_ROWS_PER_CALL,
    DATASET_MAX_ROWS_TOTAL,
)


# ── _is_dataset_goal ────────────────────────────────────────────────────────
class TestIsDatasetGoal:
    @pytest.mark.parametrize("goal", [
        "Harvest a list of mid-to-large enterprises in Phoenix, Austin, Atlanta",
        "Build a list of all SaaS companies in Austin with revenue > $3M",
        "Generate leads from local Chambers of Commerce in 5 metros",
        "Build a database of healthcare nonprofits in Maryland",
        "Produce phoenix_leads.csv — list of companies in Phoenix",
        "Scrape companies from BBB Phoenix",
        "I need a prospect list of agencies in Miami",
        "Build me a directory of consulting firms in Atlanta",
        "Harvest dataset of HealthIT vendors",
        "List of business contacts from Austin Chamber",
    ])
    def test_detects_dataset_intent(self, goal):
        assert _is_dataset_goal(goal), f"should detect dataset intent: {goal!r}"

    @pytest.mark.parametrize("goal", [
        "Write a competitive teardown of the top 5 CRM products",
        "Produce a SEO audit report for example.com",
        "Draft 5 LinkedIn posts about our launch next Tuesday",
        "Compose a brand-voice guide for the marketing team",
        "Analyse pricing trends in the AI-coding-agent market",
    ])
    def test_ignores_non_dataset_goals(self, goal):
        assert not _is_dataset_goal(goal), f"false positive on: {goal!r}"

    def test_empty_goal_is_false(self):
        assert _is_dataset_goal("") is False
        assert _is_dataset_goal(None) is False  # type: ignore[arg-type]


# ── _count_csv_data_rows ────────────────────────────────────────────────────
class TestCountCsvDataRows:
    def test_picks_largest_csv_row_count(self):
        artifacts = [
            {"original_name": "leads.csv", "csv_row_count": 12},
            {"original_name": "raw.csv", "csv_row_count": 47},
            {"original_name": "report.pdf"},  # non-CSV ignored
        ]
        assert _count_csv_data_rows(artifacts) == 47

    def test_returns_zero_when_no_csv(self):
        artifacts = [
            {"original_name": "brief.pdf"},
            {"original_name": "plan.docx"},
        ]
        assert _count_csv_data_rows(artifacts) == 0

    def test_returns_zero_for_csv_without_row_count_stamp(self):
        """Legacy write_artifact CSVs don't carry csv_row_count → 0 → gate trips."""
        artifacts = [
            {"original_name": "manual_lead_list.csv"},
        ]
        assert _count_csv_data_rows(artifacts) == 0

    def test_empty_or_none(self):
        assert _count_csv_data_rows([]) == 0
        assert _count_csv_data_rows(None) == 0  # type: ignore[arg-type]


# ── Threshold sanity ────────────────────────────────────────────────────────
class TestThresholds:
    def test_min_rows_is_user_requested_15(self):
        assert DATASET_MIN_ROWS == 15

    def test_per_call_cap_is_lower_than_total_cap(self):
        assert DATASET_MAX_ROWS_PER_CALL < DATASET_MAX_ROWS_TOTAL

    def test_per_call_cap_allows_one_full_15_row_batch(self):
        assert DATASET_MAX_ROWS_PER_CALL >= DATASET_MIN_ROWS
