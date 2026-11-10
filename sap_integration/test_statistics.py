"""Statistics from synthetic WebGUI caches and GM imports; no live SAP access."""

import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from mcp_api.tools import get_statistics
from projects.models import OtherBudgetItemTransaction, Project, SAPFund
from sap_integration.models import SAPExpenseLink, SAPImport, SAPPosition
from sap_integration.statistics import sap_statistics


def row(amount="100.00", **changes):
    return {"type": "actual", "amount": amount, "booking_date": "2026-03-01",
            "financial_position": "88000-0101", "position": "Testausgabe", **changes}


@override_settings(
    SAP_ENABLED=True, SAP_GM_IMPORT_ENABLED=False,
    SAP_BACKEND="sap_integration.backends.wuerzburg.WuerzburgWebGUIBackend",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class SAPStatisticsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            acronym="STATS", start_date=date(2024, 1, 1), end_date=date(2028, 12, 31),
            budget_total=Decimal("10000"),
        )
        cls.fund = SAPFund.objects.create(fund_number="STAT-FUND", project=cls.project)
        cls.user = get_user_model().objects.create_user("statistics-reader")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        override = override_settings(SAP_DATA_DIR=self.directory)
        override.enable()
        self.addCleanup(override.disable)

    def write(self, rows, year=2026, funds=None, schema=2):
        folder = self.directory / "processed"
        folder.mkdir(exist_ok=True)
        path = folder / f"{year}.json"
        path.write_text(json.dumps({
            "schema_version": schema, "year": year,
            "funds": funds if funds is not None else {
                self.fund.fund_number: {"has_budget": True, "transactions": rows},
            },
        }))
        return path

    def gm_import(self, fund=None, amount="200.00", year=2026):
        sap_import = SAPImport.objects.create(fund=fund or self.fund, file_name="synthetic.xlsx")
        SAPPosition.objects.create(
            sap_import=sap_import, reference="SYNTHETIC", kind="other", cost_type="TEST",
            actual=Decimal(amount), bookings=[{"date": f"{year}-03-01", "value_type": "99", "amount": amount}],
        )
        return sap_import

    def test_unlinked_actuals_include_signed_corrections_and_exclude_commitments_and_income(self):
        path = self.write([
            row("850.00"), row("-50.00", position="Korrektur"),
            row("1000.00", type="commitment"),
            row("-4000.00", financial_position="28000-0101", position="Mittelabruf"),
        ])
        before = path.read_bytes()
        self.assertEqual(sap_statistics().actuals, {"2026": Decimal("800.00")})
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)
        self.assertEqual(SAPExpenseLink.objects.count(), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_years_salary_month_and_bookings_are_not_merged_by_description(self):
        self.write([row("10.00", booking_date="2024-12-31")], year=2024)
        self.write([
            row("100.00", booking_date="2026-01-10", position="Gehalt Dezember 2025"),
            row("200.00", booking_date="2026-03-01"),
            row("30.00", booking_date="2026-01-15", position="Testausgabe"),
        ])
        self.assertEqual(sap_statistics().actuals, {
            "2024": Decimal("10.00"), "2025": Decimal("100.00"), "2026": Decimal("230.00"),
        })
        self.write([row("400.00")])
        self.assertEqual(sap_statistics().actuals["2026"], Decimal("400.00"))

    def test_only_active_project_funds_are_counted_once(self):
        inactive = SAPFund.objects.create(fund_number="INACTIVE", project=self.project, is_active=False)
        universal = SAPFund.objects.create(fund_number="UNIVERSAL", is_universal=True)
        self.write([], funds={
            self.fund.fund_number: {"transactions": [row("10.00")]},
            inactive.fund_number: {"transactions": [row("1000.00")]},
            universal.fund_number: {"transactions": [row("1000.00")]},
            "NOT-CONFIGURED": {"transactions": [row("1000.00")]},
        })
        self.assertEqual(sap_statistics().actuals, {"2026": Decimal("10.00")})

    def test_funding_setting_respects_counter_bookings_and_remaining_negative_funding(self):
        self.fund.treat_negative_actuals_as_funding = True
        self.fund.save()
        self.write([
            row("500.00"), row("-500.00", position="Storno"),
            row("100.00"), row("-900.00", position="Zufluss"), row("not-money"),
        ])
        data = sap_statistics()
        self.assertEqual(data.actuals, {"2026": Decimal("100.00")})
        self.assertIn("1 Ist-Buchung(en) mit ungültigem Betrag", " ".join(data.notes))

    def test_zero_actuals_and_missing_data_are_distinguished(self):
        self.assertEqual(sap_statistics().actuals, {})
        self.assertTrue(sap_statistics().notes)
        self.write([row(type="commitment")])
        self.assertEqual(sap_statistics().actuals, {"2026": Decimal("0.00")})
        self.write([], funds={self.fund.fund_number: {"has_budget": False, "transactions": []}})
        self.assertEqual(sap_statistics().actuals, {})

    def test_legacy_cache_and_corrupt_historical_cache(self):
        self.write([row("70.00")], schema=1)
        bad = self.directory / "processed" / "2025.json"
        bad.write_text("not-json")
        data = sap_statistics()
        self.assertEqual(data.actuals, {"2026": Decimal("70.00")})
        self.assertIn("2025", " ".join(data.notes))
        self.assertIn("parse_sap", " ".join(data.notes))

    def test_missing_dates_use_export_year_and_invalid_amounts_are_reported(self):
        self.write([row("10.00", booking_date=None), row("NaN"), row("not-money")])
        data = sap_statistics()
        self.assertEqual(data.actuals, {"2026": Decimal("10.00")})
        self.assertIn("ohne gültiges Buchungsdatum", " ".join(data.notes))
        self.assertIn("2 Ist-Buchung(en) mit ungültigem Betrag", " ".join(data.notes))

    @override_settings(SAP_ENABLED=False, SAP_GM_IMPORT_ENABLED=True)
    def test_gm_only_uses_latest_import_and_preserves_payroll_and_income_handling(self):
        self.write([row("9000.00")])  # A disabled WebGUI cache must not leak in.
        self.gm_import(amount="999.00")
        latest = self.gm_import(amount="120.00")
        SAPPosition.objects.create(sap_import=latest, reference="SALARY", kind="staff", cost_type="TEST",
                                   monthly_actuals={"2025-12": "300.00"})
        SAPPosition.objects.create(sap_import=latest, reference="INCOME", kind="income", cost_type="TEST",
                                   bookings=[{"date": "2026-03-01", "value_type": "99", "amount": "-5000.00"}])
        SAPPosition.objects.create(sap_import=latest, reference="RESERVATION", kind="other", cost_type="TEST",
                                   bookings=[{"date": "2026-03-01", "value_type": "81", "amount": "5000.00"}])
        data = sap_statistics()
        self.assertEqual(data.actuals, {"2025": Decimal("300.00"), "2026": Decimal("120.00")})
        self.assertEqual(data.sources, ["Projektexporte (GM)"])

    @override_settings(SAP_GM_IMPORT_ENABLED=True)
    def test_both_sources_use_webgui_per_fund_year_and_gm_for_other_data(self):
        self.write([row("100.00")])
        same = self.gm_import(amount="100.00")
        SAPPosition.objects.create(sap_import=same, reference="OLDER", kind="other", cost_type="TEST",
                                   bookings=[{"date": "2025-01-01", "value_type": "99", "amount": "10.00"}])
        other = SAPFund.objects.create(fund_number="GM-ONLY", project=self.project)
        self.gm_import(fund=other, amount="200.00")
        data = sap_statistics()
        self.assertEqual(data.actuals, {"2025": Decimal("10.00"), "2026": Decimal("300.00")})
        self.assertEqual(len(data.sources), 2)
        self.assertIn("Vorrang", " ".join(data.notes))

    @override_settings(SAP_ENABLED=False, SAP_GM_IMPORT_ENABLED=False)
    def test_disabled_integrations_do_not_read_stale_data(self):
        self.write([row("100.00")])
        self.gm_import()
        data = sap_statistics()
        self.assertEqual((data.actuals, data.sources), ({}, []))

    def test_page_and_mcp_show_same_actuals_without_planning_import(self):
        self.write([row("850.00"), row(type="commitment", amount="1000.00")])
        self.client.force_login(self.user)
        response = self.client.get(reverse("statistics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Finanzpositions-Exporte (WebGUI)")
        self.assertContains(response, "unabhängig von der Übernahme")
        web_rows = {r["year"]: r for r in response.context["comparison_rows"]}
        self.assertEqual(web_rows["2026"]["actual"], Decimal("850.00"))
        self.assertIsNone(web_rows["2027"]["actual"])
        self.assertEqual(response.context["comparison_actual"], [None, None, 850.0, None, None])
        self.assertEqual(response.context["funds_split"]["actual"], Decimal("850.00"))
        mcp_rows = {r["year"]: r for r in get_statistics(self.user)["years"]}
        self.assertEqual(mcp_rows["2026"]["sap_actual"], 850.0)
        self.assertIsNone(mcp_rows["2027"]["sap_actual"])
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)

    def test_page_without_cache_explains_missing_data(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("statistics"))
        self.assertContains(response, "keine SAP-Ist-Daten vorhanden")
        self.assertContains(response, "parse_sap")
        self.assertIsNone(response.context["funds_split"]["actual"])
