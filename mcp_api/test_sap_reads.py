"""SAP MCP read tests using invented funds, financial codes and bookings."""

import json
import tempfile
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from mcp_api.models import ApiToken
from projects.models import OtherBudgetItem, OtherBudgetItemTransaction, Project, SAPFund
from sap_integration.models import SAPExpenseBudgetMapping, SAPExpenseCategory, SAPExpenseLink, SAPImport, SAPPosition


def row(**changes):
    return {"type": "actual", "financial_position": "88000-0101", "financial_center": "1000",
            "gl_account": "6000", "document_number": "TEST-001", "document_type": "KR", "document_item": "0",
            "purchase_document": "", "reference": "REF-001", "reversal_document": "",
            "header_text": "Testreise", "item_text": "Bahn", "position": "Testreise Bahn",
            "business_partner": "Testanbieter", "amount": "400.00", "booking_date": "2026-03-01", **changes}


@override_settings(SAP_ENABLED=True, SAP_GM_IMPORT_ENABLED=False,
                   SAP_BACKEND="sap_integration.backends.wuerzburg.WuerzburgWebGUIBackend")
class SAPReadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("sap-reader", password="x")
        token = ApiToken(user=cls.user, name="Test")
        cls.token = token.set_new_key()
        token.save()
        cls.project = Project.objects.create(acronym="SAPTEST", start_date=date(2024, 1, 1), end_date=date(2028, 12, 31), budget_total=5000)
        cls.fund = SAPFund.objects.create(fund_number="READ-FUND", project=cls.project)
        cls.budget = OtherBudgetItem.objects.create(project=cls.project, title="Reisen", amount=3000)
        cls.category = SAPExpenseCategory.objects.create(key="travel", label="Reisekosten", kind="travel", exact_codes=["88000-0101"])
        SAPExpenseCategory.objects.create(key="staff_example", label="Personal (Test)", kind="staff", exact_codes=["42900-0201"])
        SAPExpenseBudgetMapping.objects.create(fund=cls.fund, category=cls.category, budget_item=cls.budget)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        override = override_settings(SAP_DATA_DIR=self.directory)
        override.enable()
        self.addCleanup(override.disable)
        self.write()

    def write(self, rows=None, year=2026, schema=2, funds=None):
        rows = rows if rows is not None else [row(), row(type="commitment", document_number="TEST-002", amount="125.00"),
                                            row(document_number="TEST-003", financial_position="42900-0201", amount="600.00", position="Gehalt März 2026")]
        values = {"fund_number": self.fund.fund_number, "has_budget": True, "budget": "3000.00",
                  "actual_total": "1000.00", "commitments_total": "125.00", "combined_total": "1125.00", "remaining": "1875.00",
                  "transactions": rows}
        folder = self.directory / "processed"
        folder.mkdir(exist_ok=True)
        path = folder / f"{year}.json"
        path.write_text(json.dumps({"schema_version": schema, "year": year, "generated_at": f"{year}-11-01T12:00:00+00:00",
                                    "funds": funds if funds is not None else {self.fund.fund_number: values}}))
        return path

    def rpc(self, method, params=None):
        return self.client.post("/mcp/", json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}),
                                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}").json()["result"]

    def call(self, name, arguments=None):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        text = result["content"][0]["text"]
        return result, text if result["isError"] else json.loads(text)

    def test_status_instructions_and_tool_descriptions_identify_active_source_without_credentials(self):
        with override_settings(SAP_USER="sensitive-test-user", SAP_PASSWORD="sensitive-test-password", SAP_URL="https://private.example.test"):
            result, data = self.call("get_sap_status")
        self.assertFalse(result["isError"])
        self.assertEqual(data["enabled_sources"], ["wuerzburg"])
        self.assertEqual(data["default_source"], "wuerzburg")
        self.assertEqual(data["wuerzburg"]["available_years"], [2026])
        self.assertFalse(data["gm"]["enabled"])
        self.assertNotIn("sensitive-test", json.dumps(data))
        self.assertNotIn("private.example.test", json.dumps(data))
        initialized = self.rpc("initialize")
        self.assertIn("Aktive SAP-Verfahren: wuerzburg", initialized["instructions"])
        self.assertIn("keine GM-Exceldateien anfordern", initialized["instructions"])
        tools = {t["name"]: t for t in self.rpc("tools/list")["tools"]}
        self.assertTrue(tools["get_sap_statement"]["annotations"]["readOnlyHint"])
        self.assertNotIn("import_sap_export", tools)
        from mcp_api.tools import TOOLS

        self.assertIn("Nur SAP Grants Management", TOOLS["import_sap_export"].description)

    def test_statement_reads_budgets_actuals_commitments_personnel_and_document_metadata(self):
        result, data = self.call("get_sap_statement", {"acronym": "saptest"})
        self.assertFalse(result["isError"], data)
        self.assertEqual(data["source"], "wuerzburg")
        self.assertEqual(data["year"], 2026)
        self.assertEqual(data["generated_at"], "2026-11-01T12:00:00+00:00")
        self.assertEqual((data["funds"][0]["budget"], data["funds"][0]["actual_total"], data["funds"][0]["commitments_total"]), (3000.0, 1000.0, 125.0))
        self.assertEqual(data["total_transactions"], 3)
        self.assertEqual(data["transactions"][0]["financial_position"], "88000-0101")
        self.assertEqual(data["transactions"][0]["document_number"], "TEST-001")
        self.assertEqual(data["transactions"][0]["reference"], "REF-001")
        self.assertEqual(data["transactions"][2]["category"]["kind"], "staff")
        self.assertTrue(data["document_fields_available"])

    def test_statement_preserves_duplicate_rows_and_paginates_filtered_results(self):
        self.write(rows=[row(), row(), row(type="commitment", amount="125.00")])
        _, data = self.call("get_sap_statement", {"fund_number": self.fund.fund_number, "transaction_type": "actual", "financial_position": "88000-0101", "limit": 1})
        self.assertEqual(data["total_transactions"], 2)
        self.assertEqual(len(data["transactions"]), 1)
        self.assertTrue(data["has_more"])
        _, second = self.call("get_sap_statement", {"fund_number": self.fund.fund_number, "transaction_type": "actual", "offset": 1, "limit": 1})
        self.assertEqual(second["transactions"], data["transactions"])
        self.assertFalse(second["has_more"])

    def test_project_and_fund_filters_do_not_return_other_projects(self):
        other = Project.objects.create(acronym="OTHER", start_date=date(2024, 1, 1), end_date=date(2028, 12, 31), budget_total=1000)
        fund = SAPFund.objects.create(fund_number="OTHER-FUND", project=other)
        path = self.directory / "processed" / "2026.json"
        payload = json.loads(path.read_text())
        payload["funds"][fund.fund_number] = payload["funds"][self.fund.fund_number]
        path.write_text(json.dumps(payload))
        _, data = self.call("get_sap_statement", {"acronym": "SAPTEST"})
        self.assertEqual({r["fund_number"] for r in data["transactions"]}, {self.fund.fund_number})
        result, _ = self.call("get_sap_statement", {"acronym": "SAPTEST", "fund_number": fund.fund_number})
        self.assertTrue(result["isError"])

    def test_default_year_uses_latest_available_project_data(self):
        self.write(rows=[row(booking_date="2025-03-01")], year=2025)
        self.write(funds={})
        _, data = self.call("get_sap_statement", {"acronym": "SAPTEST"})
        self.assertEqual(data["year"], 2025)
        self.assertEqual(data["available_years"], [2026, 2025])

    def test_original_signed_rows_and_adjusted_display_totals_are_distinct(self):
        self.fund.treat_negative_actuals_as_funding = True
        self.fund.save()
        path = self.write(rows=[row(), row(amount="-250.00", document_number="INCOME", financial_position="28000-0301",
                                            position="Testmittelzufluss", business_partner="Testförderer")])
        payload = json.loads(path.read_text())
        payload["funds"][self.fund.fund_number].update(actual_total="150.00", commitments_total="0.00", combined_total="150.00", remaining="2850.00")
        path.write_text(json.dumps(payload))
        _, data = self.call("get_sap_statement")
        self.assertEqual(data["funds"][0]["actual_total"], 150.0)
        self.assertEqual(data["funds"][0]["adjusted_display_totals"]["actual_total"], 400.0)
        self.assertEqual(data["funds"][0]["adjusted_display_totals"]["funding_total"], 250.0)
        self.assertEqual(data["transactions"][1]["amount"], -250.0)

    def test_absent_fund_data_is_not_reported_as_a_zero_balance(self):
        self.write(funds={})
        _, data = self.call("get_sap_statement", {"acronym": "SAPTEST", "year": 2026})
        self.assertFalse(data["funds"][0]["has_data"])
        self.assertIsNone(data["funds"][0]["actual_total"])
        self.assertEqual(data["transactions"], [])

    def test_reconciliation_auto_reads_webgui_without_requesting_gm_exports(self):
        result, data = self.call("get_sap_reconciliation", {"acronym": "SAPTEST"})
        self.assertFalse(result["isError"], data)
        self.assertEqual(data["sources"], ["wuerzburg"])
        self.assertEqual(data["funds"][0]["source"], "wuerzburg")
        positions = data["funds"][0]["open_positions"]
        self.assertEqual(len(positions), 2)
        self.assertEqual({p["status"] for p in positions}, {"Fehlt in Planung"})
        self.assertTrue(all(p["source_key"] for p in positions))
        self.assertTrue(all(p["budget_item_id"] == self.budget.pk for p in positions))

    def test_gm_response_is_preserved_and_both_sources_are_explicitly_separated(self):
        imported = SAPImport.objects.create(fund=self.fund, file_name="test-export.xlsx", last_booking=date(2026, 3, 1))
        SAPPosition.objects.create(sap_import=imported, reference="GM-TEST", kind="travel", cost_type="TEST", title="Testreise", actual=50)
        with override_settings(SAP_ENABLED=False, SAP_GM_IMPORT_ENABLED=True):
            result, data = self.call("get_sap_reconciliation", {"acronym": "SAPTEST"})
            self.assertFalse(result["isError"], data)
            self.assertEqual(data["funds"][0]["psp_element"], self.fund.fund_number)
            self.assertEqual(data["funds"][0]["open_positions"][0]["sap_reference"], "GM-TEST")
            self.assertEqual(data["funds"][0]["open_positions"][0]["actual"], 50.0)
            self.assertTrue(self.call("get_sap_statement")[0]["isError"])
        with override_settings(SAP_GM_IMPORT_ENABLED=True):
            _, data = self.call("get_sap_reconciliation", {"acronym": "SAPTEST"})
            self.assertEqual({f["source"] for f in data["funds"]}, {"gm", "wuerzburg"})
            _, selected = self.call("get_sap_reconciliation", {"acronym": "SAPTEST", "source": "gm"})
            self.assertEqual({f["source"] for f in selected["funds"]}, {"gm"})

    def test_legacy_cache_can_be_read_and_reconciliation_requests_offline_reparse(self):
        legacy_row = {key: row()[key] for key in ("type", "business_partner", "position", "amount", "booking_date")}
        self.write(rows=[legacy_row], schema=1)
        result, data = self.call("get_sap_statement")
        self.assertFalse(result["isError"])
        self.assertFalse(data["document_fields_available"])
        self.assertIn("parse_sap", data["notice"])
        self.assertIsNone(data["transactions"][0]["financial_position"])
        result, data = self.call("get_sap_reconciliation", {"acronym": "SAPTEST"})
        self.assertFalse(result["isError"])
        self.assertIn("parse_sap", data["funds"][0]["error"])

    def test_invalid_filters_and_missing_cache_give_actionable_errors(self):
        for arguments in ({"limit": 0}, {"offset": -1}, {"year": True}, {"year": "2026"}, {"transaction_type": "unknown"}):
            self.assertTrue(self.call("get_sap_statement", arguments)[0]["isError"])
        (self.directory / "processed" / "2026.json").unlink()
        result, message = self.call("get_sap_reconciliation", {"acronym": "SAPTEST"})
        self.assertTrue(result["isError"])
        self.assertIn("parse_sap", message)
        self.assertIn("Kein GM-Projektexport erforderlich", message)

    def test_read_tools_do_not_write_cache_imports_or_planning(self):
        path = self.directory / "processed" / "2026.json"
        before = path.read_bytes()
        for tool, args in (("get_sap_status", {}), ("get_sap_statement", {}), ("get_sap_reconciliation", {"acronym": "SAPTEST"})):
            result, data = self.call(tool, args)
            self.assertFalse(result["isError"], data)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(SAPImport.objects.count(), 0)
        self.assertEqual(SAPPosition.objects.count(), 0)
        self.assertEqual(SAPExpenseLink.objects.count(), 0)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)
