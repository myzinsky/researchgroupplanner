import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from mcp_api.models import ApiToken
from projects.models import Project, ProjectDeliverable, StaffBudgetItem
from projects.models import SAPFund
from sap_integration.models import SAPImport, SAPPosition
from staffing.models import (
    Employment,
    EmploymentSalaries,
    Rebooking,
    SalaryCategory,
    SalaryTable,
    SalaryTableEntry,
    StaffFundingAllocation,
    StaffMember,
)


@override_settings(SAP_ENABLED=False, SAP_GM_IMPORT_ENABLED=False)
class McpEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("reader", password="x")
        self.staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.token = self.make_token(self.user)
        self.staff_token = self.make_token(self.staff)
        self.project = Project.objects.create(acronym="MCPX", start_date=date(2026, 1, 1), end_date=date(2028, 12, 31),
                                              budget_total=Decimal("100000"))
        self.other = Project.objects.create(acronym="OTHR", start_date=date(2026, 1, 1), end_date=date(2028, 12, 31),
                                            budget_total=Decimal("100000"))
        self.item = StaffBudgetItem.objects.create(project=self.project, title="WiMi", amount=Decimal("80000"))
        self.other_item = StaffBudgetItem.objects.create(project=self.other, title="WiMi", amount=Decimal("80000"))
        ProjectDeliverable.objects.create(project=self.project, title="D1", deadline=date(2027, 6, 30), is_lead=True)
        self.member = StaffMember.objects.create(first_name="Erika", last_name="Muster")
        self.employment = employment = Employment.objects.create(staff_member=self.member, start_date=date(2026, 1, 1),
                                               end_date=date(2027, 12, 31), percentage=Decimal("100"))
        self.allocation = StaffFundingAllocation.objects.create(
            employment=employment, budget_item=self.item, percentage=Decimal("100"), start_date=date(2026, 1, 1),
        )

    def make_token(self, user):
        api_token = ApiToken(user=user, name="Test")
        plain = api_token.set_new_key()
        api_token.save()
        return plain

    def rpc(self, method, params=None, token=None, message_id=1):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if message_id is not None:
            body["id"] = message_id
        return self.client.post("/mcp/", json.dumps(body), content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    def call(self, name, arguments=None, token=None):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, token=token)
        result = response.json()["result"]
        return result, (json.loads(result["content"][0]["text"]) if not result["isError"] else result["content"][0]["text"])

    def test_requires_valid_token(self):
        response = self.client.post("/mcp/", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="mcp"')
        ApiToken.objects.filter(user=self.user).update(is_active=False)
        self.assertEqual(self.rpc("ping").status_code, 401)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get("/mcp/", HTTP_AUTHORIZATION=f"Bearer {self.token}").status_code, 405)

    def test_initialize_and_notification(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}).json()["result"]

        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(self.rpc("notifications/initialized", message_id=None).status_code, 202)
        self.assertIsNotNone(ApiToken.objects.get(user=self.user).last_used_at)

    def test_tools_list_hides_write_tools_for_non_staff(self):
        reader = {t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]}
        staff = {t["name"] for t in self.rpc("tools/list", token=self.staff_token).json()["result"]["tools"]}

        self.assertIn("get_project", reader)
        self.assertNotIn("create_rebooking", reader)
        self.assertIn("create_rebooking", staff)

    def test_unknown_method(self):
        self.assertEqual(self.rpc("resources/list").json()["error"]["code"], -32601)

    def test_read_tools(self):
        _, projects = self.call("list_projects", {"status": "all"})
        self.assertIn("MCPX", [p["acronym"] for p in projects["projects"]])
        _, project = self.call("get_project", {"acronym": "mcpx"})
        self.assertEqual(project["staff_allocations"][0]["allocation_id"], self.allocation.id)
        self.assertEqual(project["deliverables"], [{"title": "D1", "deadline": "2027-06-30", "lead": True}])
        _, person = self.call("get_staff_member", {"name": "Erika"})
        self.assertEqual(person["employments"][0]["allocations"][0]["allocation_id"], self.allocation.id)
        for name in ("list_staff", "list_budget_items", "list_rebookings", "list_warnings", "list_deliverables",
                     "get_statistics"):
            result, _ = self.call(name)
            self.assertFalse(result["isError"], name)
        result, message = self.call("get_project", {"acronym": "NOPE"})
        self.assertTrue(result["isError"])
        self.assertIn("nicht gefunden", message)

    def test_write_tools_require_staff(self):
        arguments = {"allocation_id": self.allocation.id, "budget_item_id": self.other_item.id,
                     "percentage": 50, "start_date": "2026-07-01"}

        result, _ = self.call("create_rebooking", arguments)
        self.assertTrue(result["isError"])
        self.assertFalse(Rebooking.objects.exists())

        result, data = self.call("create_rebooking", arguments, token=self.staff_token)
        self.assertFalse(result["isError"])
        rebooking_id = data["created"]["rebooking_id"]
        result, _ = self.call("delete_rebooking", {"rebooking_id": rebooking_id}, token=self.staff_token)
        self.assertFalse(Rebooking.objects.exists())

    def test_invalid_rebooking_is_reported(self):
        result, message = self.call("create_rebooking", {
            "allocation_id": self.allocation.id, "budget_item_id": self.other_item.id,
            "percentage": 50, "start_date": "2030-01-01",
        }, token=self.staff_token)

        self.assertTrue(result["isError"])
        self.assertIn("Zuordnung", message)

    def test_plan_employment(self):
        result, data = self.call("plan_employment", {
            "first_name": "Neue", "last_name": "Person", "budget_item_id": self.item.id, "percentage": 50,
            "start_date": "2027-01-01", "end_date": "2027-12-31",
        }, token=self.staff_token)

        self.assertFalse(result["isError"], data)
        employment = Employment.objects.get(id=data["employment_id"])
        self.assertEqual((employment.status, str(employment.staff_member)), ("planned", "Neue Person"))
        self.assertFalse(data["salaries_estimated"])

    def test_ignore_warning(self):
        _, warnings = self.call("list_warnings")
        key = warnings["warnings"][0]["key"]

        result, _ = self.call("ignore_warning", {"key": key, "comment": "Bekannt"}, token=self.staff_token)

        self.assertFalse(result["isError"])
        _, after = self.call("list_warnings", {"include_ignored": True})
        self.assertNotIn(key, [w["key"] for w in after["warnings"]])
        self.assertIn(key, [w["key"] for w in after["ignored_warnings"]])

    # --- update_employment / delete_employment ---

    def add_salary_table(self):
        e13 = SalaryCategory.objects.create(name="E13")
        table = SalaryTable.objects.create(name="TV-L 2026", valid_from=date(2026, 1, 1))
        SalaryTableEntry.objects.create(table=table, salary_category=e13, level=1, amount=Decimal("6000.00"))
        SalaryTableEntry.objects.create(table=table, salary_category=e13, level=2, amount=Decimal("6400.00"))

    def add_sap_actuals(self, months):
        self.allocation.sap_reference = "4000123"
        self.allocation.save()
        fund = SAPFund.objects.create(fund_number="1/070000001", project=self.project)
        sap_import = SAPImport.objects.create(fund=fund, file_name="export.xlsx")
        SAPPosition.objects.create(sap_import=sap_import, reference="4000123", kind="staff", cost_type="812000",
                                   monthly_actuals={month: "5000.00" for month in months})

    def test_update_employment_reestimates_salaries(self):
        self.add_salary_table()

        result, data = self.call("update_employment", {
            "employment_id": self.employment.id, "salary_category": "e13", "start_level": 1, "percentage": 50,
        }, token=self.staff_token)

        self.assertFalse(result["isError"], data)
        self.assertTrue(data["salaries_estimated"])
        employment = data["employment"]
        self.assertEqual((employment["percentage"], employment["salary_category"]), (50.0, "E13"))
        self.assertEqual(employment["level_start_date"], "2026-01-01")
        self.assertEqual(employment["monthly_costs"]["2026-05"]["planned"], 3000.0)
        self.assertEqual(employment["allocations"][0]["allocation_id"], self.allocation.id)
        self.assertEqual(data["cost_changes"][0]["budget_item_id"], self.item.id)
        self.assertGreater(data["cost_changes"][0]["change"], 0)

    def test_update_employment_keeps_months_with_sap_actuals(self):
        self.add_salary_table()
        EmploymentSalaries.objects.create(employment=self.employment, salary=Decimal("5000.00"),
                                          start_date=date(2026, 1, 1), end_date=date(2027, 12, 31))
        self.add_sap_actuals(["2026-01", "2026-02"])

        result, data = self.call("update_employment", {
            "employment_id": self.employment.id, "salary_category": "E13", "start_level": 1,
        }, token=self.staff_token)

        self.assertFalse(result["isError"], data)
        costs = data["employment"]["monthly_costs"]
        self.assertEqual(data["salaries_kept_until"], "2026-02")
        self.assertEqual((costs["2026-01"]["planned"], costs["2026-02"]["planned"]), (5000.0, 5000.0))
        self.assertEqual(costs["2026-01"]["sap_actual"], 5000.0)
        self.assertEqual(costs["2026-03"]["planned"], 6000.0)

    def test_update_employment_validation_errors(self):
        cases = [
            ({"end_date": "2025-12-31"}, "Ende"),
            ({"salary_category": "E99"}, "nicht gefunden"),
            ({"percentage": 0}, "percentage"),
            ({"status": "unknown"}, "status"),
            ({"level_start_date": "2026-06-01", "start_level": 1}, "Stufenbeginn"),
            ({"salary": 5}, "Unbekannte Felder"),
            ({}, "Keine Änderungen"),
        ]
        self.add_salary_table()
        cases.append(({"salary_category": "E13"}, "Stufe"))
        for arguments, expected in cases:
            result, message = self.call("update_employment", {"employment_id": self.employment.id, **arguments},
                                        token=self.staff_token)
            self.assertTrue(result["isError"], arguments)
            self.assertIn(expected, message, arguments)
        result, message = self.call("update_employment", {"employment_id": 999999, "percentage": 50},
                                    token=self.staff_token)
        self.assertIn("Keine Anstellung", message)
        self.employment.refresh_from_db()
        self.assertEqual((self.employment.end_date, self.employment.percentage), (date(2027, 12, 31), Decimal("100")))

    def test_update_employment_allocations_outside_period(self):
        early = StaffFundingAllocation.objects.create(
            employment=self.employment, budget_item=self.other_item, percentage=Decimal("20"),
            start_date=date(2026, 1, 1), end_date=date(2026, 2, 28),
        )
        arguments = {"employment_id": self.employment.id, "start_date": "2026-03-01", "end_date": "2027-06-30"}

        result, message = self.call("update_employment", arguments, token=self.staff_token)

        self.assertTrue(result["isError"])
        self.assertIn(f"{self.allocation.id}: MCPX", message)
        self.assertIn(f"{early.id}: OTHR", message)
        self.assertIn("clip_allocations", message)
        self.assertEqual(Employment.objects.get(id=self.employment.id).start_date, date(2026, 1, 1))

        result, data = self.call("update_employment", {**arguments, "clip_allocations": True}, token=self.staff_token)

        self.assertFalse(result["isError"], data)
        self.allocation.refresh_from_db()
        self.assertEqual((self.allocation.start_date, self.allocation.end_date), (date(2026, 3, 1), None))
        self.assertFalse(StaffFundingAllocation.objects.filter(id=early.id).exists())
        self.assertEqual(len(data["deleted_allocations"]), 1)
        self.assertEqual([a["allocation_id"] for a in data["employment"]["allocations"]], [self.allocation.id])

    def test_update_employment_clips_explicit_allocation_end(self):
        self.allocation.end_date = date(2027, 12, 31)
        self.allocation.save()

        result, data = self.call("update_employment", {
            "employment_id": self.employment.id, "end_date": "2027-06-30", "clip_allocations": True,
        }, token=self.staff_token)

        self.assertFalse(result["isError"], data)
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.end_date, date(2027, 6, 30))

    def test_update_employment_dry_run(self):
        EmploymentSalaries.objects.create(employment=self.employment, salary=Decimal("5000.00"),
                                          start_date=date(2026, 1, 1), end_date=date(2027, 12, 31))

        result, data = self.call("update_employment", {
            "employment_id": self.employment.id, "end_date": "2026-12-31", "dry_run": True,
        }, token=self.staff_token)

        self.assertFalse(result["isError"], data)
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["employment"]["end"], "2026-12-31")
        self.assertEqual(data["cost_changes"], [{
            "budget_item_id": self.item.id, "source": "MCPX – WiMi", "before": 120000.0, "after": 60000.0,
            "change": -60000.0,
        }])
        self.employment.refresh_from_db()
        self.assertEqual(self.employment.end_date, date(2027, 12, 31))

    def test_delete_employment(self):
        result, message = self.call("delete_employment", {"employment_id": self.employment.id}, token=self.staff_token)
        self.assertTrue(result["isError"])
        self.assertIn("delete_allocations", message)

        result, data = self.call("delete_employment", {
            "employment_id": self.employment.id, "delete_allocations": True, "dry_run": True,
        }, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertFalse(data["deleted"])
        self.assertTrue(Employment.objects.filter(id=self.employment.id).exists())

        result, data = self.call("delete_employment", {
            "employment_id": self.employment.id, "delete_allocations": True,
        }, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertFalse(Employment.objects.filter(id=self.employment.id).exists())
        self.assertFalse(StaffFundingAllocation.objects.filter(id=self.allocation.id).exists())

    def test_delete_employment_refuses_rebookings_and_sap_actuals(self):
        rebooking = Rebooking.objects.create(allocation=self.allocation, budget_item=self.other_item,
                                             percentage=Decimal("50"), start_date=date(2026, 7, 1))
        arguments = {"employment_id": self.employment.id, "delete_allocations": True}

        result, message = self.call("delete_employment", arguments, token=self.staff_token)
        self.assertTrue(result["isError"])
        self.assertIn("Umbuchungen", message)

        rebooking.delete()
        self.add_sap_actuals(["2026-01"])
        result, message = self.call("delete_employment", arguments, token=self.staff_token)
        self.assertTrue(result["isError"])
        self.assertIn("SAP-Ist", message)
        self.assertTrue(Employment.objects.filter(id=self.employment.id).exists())

    def test_employment_write_tools_require_staff(self):
        tools = {t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertFalse({"update_employment", "delete_employment"} & tools)

        result, _ = self.call("update_employment", {"employment_id": self.employment.id, "percentage": 50})
        self.assertTrue(result["isError"])
        result, _ = self.call("delete_employment", {"employment_id": self.employment.id, "delete_allocations": True})
        self.assertTrue(result["isError"])
        self.employment.refresh_from_db()
        self.assertEqual(self.employment.percentage, Decimal("100"))

    # --- create_allocation / update_allocation / delete_allocation ---

    def test_create_allocation(self):
        self.allocation.percentage = Decimal("60")
        self.allocation.save()
        arguments = {"employment_id": self.employment.id, "budget_item_id": self.other_item.id, "percentage": 40,
                     "start_date": "2027-01-01", "end_date": "2027-06-30", "sap_reference": "4000999"}

        result, data = self.call("create_allocation", {**arguments, "dry_run": True}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual(StaffFundingAllocation.objects.count(), 1)

        result, data = self.call("create_allocation", arguments, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        created = StaffFundingAllocation.objects.get(id=data["allocation"]["allocation_id"])
        self.assertEqual((created.budget_item, created.sap_reference), (self.other_item, "4000999"))

    def test_create_allocation_rules(self):
        base = {"employment_id": self.employment.id, "budget_item_id": self.other_item.id, "percentage": 10,
                "start_date": "2027-01-01", "end_date": None}
        self.other.end_date = date(2027, 6, 30)
        self.other.save()
        cases = [
            ({}, "Projektlaufzeit"),
            ({"end_date": "2027-03-31"}, "100 %"),
            ({"start_date": "2025-12-01", "end_date": "2026-01-31"}, "Anstellung"),
            ({"end_date": "2026-12-31"}, "Enddatum"),
            ({"percentage": 120}, "percentage"),
            ({"budget_item_id": 999999}, "Personalbudget"),
        ]
        for overrides, expected in cases:
            result, message = self.call("create_allocation", {**base, **overrides}, token=self.staff_token)
            self.assertTrue(result["isError"], overrides)
            self.assertIn(expected, message, overrides)
        self.assertEqual(StaffFundingAllocation.objects.count(), 1)

    def test_update_allocation(self):
        EmploymentSalaries.objects.create(employment=self.employment, salary=Decimal("5000.00"),
                                          start_date=date(2026, 1, 1), end_date=date(2027, 12, 31))
        arguments = {"allocation_id": self.allocation.id, "budget_item_id": self.other_item.id,
                     "sap_reference": "4000123"}

        result, data = self.call("update_allocation", {**arguments, "dry_run": True}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual({c["source"]: c["change"] for c in data["cost_changes"]},
                         {"MCPX – WiMi": -120000.0, "OTHR – WiMi": 120000.0})
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.budget_item, self.item)

        result, data = self.call("update_allocation", arguments, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.allocation.refresh_from_db()
        self.assertEqual((self.allocation.budget_item, self.allocation.sap_reference), (self.other_item, "4000123"))

        result, message = self.call("update_allocation", {"allocation_id": self.allocation.id, "percentage": 50,
                                                          "salary": 1}, token=self.staff_token)
        self.assertIn("Unbekannte Felder", message)
        result, message = self.call("update_allocation", {"allocation_id": self.allocation.id,
                                                          "end_date": "2028-06-30"}, token=self.staff_token)
        self.assertIn("Anstellung", message)

    def test_allocation_tools_refuse_open_rebookings(self):
        Rebooking.objects.create(allocation=self.allocation, budget_item=self.other_item,
                                 percentage=Decimal("50"), start_date=date(2026, 7, 1))

        for name, arguments in (("update_allocation", {"percentage": 50}), ("delete_allocation", {})):
            result, message = self.call(name, {"allocation_id": self.allocation.id, **arguments},
                                        token=self.staff_token)
            self.assertTrue(result["isError"], name)
            self.assertIn("Umbuchungen", message)
        self.assertTrue(StaffFundingAllocation.objects.filter(id=self.allocation.id).exists())

    def test_delete_allocation(self):
        EmploymentSalaries.objects.create(employment=self.employment, salary=Decimal("5000.00"),
                                          start_date=date(2026, 1, 1), end_date=date(2027, 12, 31))

        result, data = self.call("delete_allocation", {"allocation_id": self.allocation.id, "dry_run": True},
                                 token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual(data["cost_changes"][0]["change"], -120000.0)
        self.assertTrue(StaffFundingAllocation.objects.filter(id=self.allocation.id).exists())

        result, data = self.call("delete_allocation", {"allocation_id": self.allocation.id}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertFalse(StaffFundingAllocation.objects.filter(id=self.allocation.id).exists())

    def test_allocation_tools_require_staff(self):
        tools = {t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertFalse({"create_allocation", "update_allocation", "delete_allocation"} & tools)

        result, _ = self.call("delete_allocation", {"allocation_id": self.allocation.id})
        self.assertTrue(result["isError"])
        self.assertTrue(StaffFundingAllocation.objects.filter(id=self.allocation.id).exists())

    # --- cost totals, update_staff_member, merge_employments ---

    def test_cost_changes_report_budget_item_totals(self):
        EmploymentSalaries.objects.create(employment=self.employment, salary=Decimal("5000.00"),
                                          start_date=date(2026, 1, 1), end_date=date(2027, 12, 31))
        # Someone else already planned on OTHR – WiMi.
        colleague = Employment.objects.create(staff_member=StaffMember.objects.create(first_name="Max", last_name="M"),
                                              start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
                                              percentage=Decimal("100"))
        EmploymentSalaries.objects.create(employment=colleague, salary=Decimal("1000.00"),
                                          start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        StaffFundingAllocation.objects.create(employment=colleague, budget_item=self.other_item,
                                              percentage=Decimal("100"), start_date=date(2026, 1, 1))
        self.allocation.percentage = Decimal("50")
        self.allocation.save()

        _, data = self.call("create_allocation", {
            "employment_id": self.employment.id, "budget_item_id": self.other_item.id, "percentage": 50,
            "start_date": "2026-01-01", "end_date": None, "dry_run": True,
        }, token=self.staff_token)
        changes = {c["source"]: (c["before"], c["after"]) for c in data["cost_changes"]}
        self.assertEqual(changes["OTHR – WiMi"], (12000.0, 72000.0))
        self.assertEqual(changes["MCPX – WiMi"], (60000.0, 60000.0))

        _, data = self.call("update_allocation", {"allocation_id": self.allocation.id,
                                                  "budget_item_id": self.other_item.id, "dry_run": True},
                            token=self.staff_token)
        changes = {c["source"]: (c["before"], c["after"]) for c in data["cost_changes"]}
        self.assertEqual(changes, {"MCPX – WiMi": (60000.0, 0.0), "OTHR – WiMi": (12000.0, 72000.0)})

        _, data = self.call("delete_allocation", {"allocation_id": self.allocation.id, "dry_run": True},
                            token=self.staff_token)
        self.assertEqual(data["cost_changes"][0]["before"], 60000.0)

    def test_update_staff_member(self):
        result, data = self.call("update_staff_member", {"staff_id": self.member.id, "status": "alumni",
                                                         "dry_run": True}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual((data["status"], data["changed"]), ("alumni", ["status"]))
        self.member.refresh_from_db()
        self.assertEqual(self.member.status, "active")

        result, data = self.call("update_staff_member", {"staff_id": self.member.id, "status": "alumni",
                                                         "email": "erika@example.org"}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.member.refresh_from_db()
        self.assertEqual((self.member.status, self.member.email), ("alumni", "erika@example.org"))
        self.assertFalse(self.member.is_leadership)

        for arguments, expected in (({"status": "former"}, "status"), ({"email": "nope"}, "email"),
                                    ({"salary": 1}, "Unbekannte Felder"), ({}, "Keine Änderungen")):
            result, message = self.call("update_staff_member", {"staff_id": self.member.id, **arguments},
                                        token=self.staff_token)
            self.assertTrue(result["isError"], arguments)
            self.assertIn(expected, message)

    def test_merge_employments(self):
        following = Employment.objects.create(staff_member=self.member, start_date=date(2028, 1, 1),
                                              end_date=date(2028, 6, 30), percentage=Decimal("100"))
        StaffFundingAllocation.objects.create(employment=following, budget_item=self.item, percentage=Decimal("100"),
                                              start_date=date(2028, 1, 1))
        arguments = {"employment_ids": [following.id, self.employment.id]}

        result, data = self.call("merge_employments", {**arguments, "dry_run": True}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual((data["employment"]["start"], data["employment"]["end"]), ("2026-01-01", "2028-06-30"))
        self.assertTrue(Employment.objects.filter(id=following.id).exists())

        result, data = self.call("merge_employments", arguments, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertFalse(Employment.objects.filter(id=following.id).exists())
        self.assertEqual(len(data["employment"]["allocations"]), 2)

    def test_merge_employments_refuses_non_mergeable(self):
        gap = Employment.objects.create(staff_member=self.member, start_date=date(2028, 2, 1),
                                        end_date=date(2028, 6, 30), percentage=Decimal("100"))

        result, message = self.call("merge_employments", {"employment_ids": [self.employment.id, gap.id]},
                                    token=self.staff_token)

        self.assertTrue(result["isError"])
        self.assertIn("nicht direkt", message)
        self.assertTrue(Employment.objects.filter(id=gap.id).exists())
        result, _ = self.call("merge_employments", {"employment_ids": [self.employment.id, gap.id]})
        self.assertTrue(result["isError"])

    # --- budget items ---

    def test_list_budget_items_shows_allowed_categories(self):
        from projects.models import OtherBudgetItem, OverheadBudgetItem, StaffBudgetItemEligibility

        StaffBudgetItemEligibility.objects.create(budget_item=self.item, eligible_employment="researcher")
        OtherBudgetItem.objects.create(project=self.project, title="Reisen", amount=Decimal("5000"))
        OverheadBudgetItem.objects.create(project=self.project, amount=Decimal("20000"))

        _, data = self.call("list_budget_items", {"acronym": "MCPX"})

        self.assertEqual(data["staff_budget_items"][0]["allowed_categories"], ["researcher"])
        self.assertEqual(data["other_budget_items"][0]["title"], "Reisen")
        self.assertEqual(data["overhead_budget_items"][0]["amount"], 20000.0)

    def test_create_budget_item(self):
        from projects.models import OverheadBudgetItem, StaffBudgetItem

        arguments = {"project": "mcpx", "kind": "staff", "title": "HiWi", "amount": 12000,
                     "allowed_categories": ["student"]}
        result, data = self.call("create_budget_item", {**arguments, "dry_run": True}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertIsNone(data["budget_item"]["budget_item_id"])
        self.assertFalse(StaffBudgetItem.objects.filter(title="HiWi").exists())

        result, data = self.call("create_budget_item", arguments, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        item = StaffBudgetItem.objects.get(id=data["budget_item"]["budget_item_id"])
        self.assertEqual(list(item.get_eligibilities().values_list("eligible_employment", flat=True)), ["student"])

        result, data = self.call("create_budget_item", {"project": "MCPX", "kind": "overhead", "amount": 25000},
                                 token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual(OverheadBudgetItem.objects.get(project=self.project).amount, Decimal("25000"))

        for overrides, expected in (({"kind": "x"}, "kind"), ({"title": ""}, "Titel"), ({"amount": "abc"}, "amount"),
                                    ({"allowed_categories": ["boss"]}, "Unbekannte Kategorien"),
                                    ({"project": "NOPE"}, "nicht gefunden"),
                                    ({"kind": "other", "allowed_categories": ["student"]}, "nur bei Personal")):
            result, message = self.call("create_budget_item", {**arguments, **overrides}, token=self.staff_token)
            self.assertTrue(result["isError"], overrides)
            self.assertIn(expected, message, overrides)

    def test_update_budget_item(self):
        from projects.models import StaffBudgetItemEligibility

        StaffBudgetItemEligibility.objects.create(budget_item=self.item, eligible_employment="researcher")
        arguments = {"budget_item_id": self.item.id, "amount": 90000,
                     "allowed_categories": ["researcher", "undergrad"]}

        result, data = self.call("update_budget_item", {**arguments, "dry_run": True}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual(data["budget_item"]["allowed_categories"], ["researcher", "undergrad"])
        self.item.refresh_from_db()
        self.assertEqual((self.item.amount, self.item.get_eligibilities().count()), (Decimal("80000"), 1))

        result, data = self.call("update_budget_item", arguments, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.item.refresh_from_db()
        self.assertEqual((self.item.amount, self.item.title), (Decimal("90000"), "WiMi"))
        self.assertEqual(self.item.get_eligibilities().count(), 2)

        result, message = self.call("update_budget_item", {"budget_item_id": self.item.id}, token=self.staff_token)
        self.assertIn("Keine Änderungen", message)
        result, message = self.call("update_budget_item", {"budget_item_id": self.item.id, "kind": "other",
                                                           "amount": 1}, token=self.staff_token)
        self.assertIn("Kein Budgetposten", message)

    def test_budget_item_tools_require_staff(self):
        result, _ = self.call("update_budget_item", {"budget_item_id": self.item.id, "amount": 1})
        self.assertTrue(result["isError"])
        result, _ = self.call("create_budget_item", {"project": "MCPX", "kind": "overhead", "amount": 1})
        self.assertTrue(result["isError"])
        self.item.refresh_from_db()
        self.assertEqual(self.item.amount, Decimal("80000"))

    # --- delete_staff_member ---

    def test_delete_staff_member(self):
        result, message = self.call("delete_staff_member", {"staff_id": self.member.id}, token=self.staff_token)
        self.assertTrue(result["isError"])
        self.assertIn("Anstellungen", message)
        self.assertIn("Zuordnungen", message)

        lone = StaffMember.objects.create(first_name="Ohne", last_name="Alles")
        result, data = self.call("delete_staff_member", {"staff_id": lone.id, "dry_run": True}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertTrue(StaffMember.objects.filter(id=lone.id).exists())

        result, data = self.call("delete_staff_member", {"staff_id": lone.id}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertFalse(StaffMember.objects.filter(id=lone.id).exists())

    def test_delete_staff_member_refuses_sap_data(self):
        from sap_integration.models import SAPPersonMapping

        mapped = StaffMember.objects.create(first_name="Map", last_name="Ped")
        SAPPersonMapping.objects.create(sap_name="Ped, Map", staff_member=mapped)
        named = StaffMember.objects.create(first_name="Ronja", last_name="Reserviert")
        fund = SAPFund.objects.create(fund_number="1/070000002", project=self.project)
        sap_import = SAPImport.objects.create(fund=fund, file_name="export.xlsx")
        SAPPosition.objects.create(sap_import=sap_import, reference="4000777", kind="staff", cost_type="812000",
                                   person_name="Reserviert, Ronja", commitment=Decimal("1000"))

        for member, expected in ((mapped, "SAP-Namenszuordnungen"), (named, "SAP-Mittelreservierungen")):
            result, message = self.call("delete_staff_member", {"staff_id": member.id}, token=self.staff_token)
            self.assertTrue(result["isError"], member)
            self.assertIn(expected, message)
            self.assertTrue(StaffMember.objects.filter(id=member.id).exists())

        result, _ = self.call("delete_staff_member", {"staff_id": mapped.id})
        self.assertTrue(result["isError"])

    # --- SHK hourly rates and manual salaries ---

    def add_student_rates(self):
        from staffing.models import StudentHourlyRate

        StudentHourlyRate.objects.all().delete()  # rates from the data migration
        StudentHourlyRate.objects.create(has_bachelor=False, hourly_rate=Decimal("15.00"),
                                         employer_share=Decimal("20"), valid_from=date(2026, 1, 1))
        StudentHourlyRate.objects.create(has_bachelor=True, hourly_rate=Decimal("17.50"),
                                         employer_share=Decimal("20"), valid_from=date(2026, 1, 1),
                                         valid_until=date(2026, 12, 31))

    def test_plan_student_employment_uses_hourly_rate(self):
        self.add_student_rates()

        result, data = self.call("plan_employment", {
            "first_name": "Stu", "last_name": "Dent", "budget_item_id": self.item.id, "percentage": 25,
            "start_date": "2026-11-01", "end_date": "2027-01-31", "category": "student", "has_bachelor": True,
        }, token=self.staff_token)

        self.assertFalse(result["isError"], data)
        self.assertTrue(data["salaries_estimated"])
        # 10 h/week × 4.348 × 17.50 € × 1.2; no bachelor rate for 2027.
        self.assertEqual(data["months_without_table_amount"], ["2027-01"])
        employment = Employment.objects.get(id=data["employment_id"])
        self.assertTrue(employment.has_bachelor)
        _, person = self.call("get_staff_member", {"staff_id": data["staff_id"]})
        costs = person["employments"][0]["monthly_costs"]
        self.assertEqual((costs["2026-11"]["planned"], costs["2027-01"]["planned"]), (913.08, 0.0))

    def test_update_employment_switches_student_rate(self):
        self.add_student_rates()

        result, data = self.call("update_employment", {
            "employment_id": self.employment.id, "category": "student", "percentage": 25,
        }, token=self.staff_token)

        self.assertFalse(result["isError"], data)
        self.assertTrue(data["salaries_estimated"])
        self.assertEqual(data["employment"]["monthly_costs"]["2026-03"]["planned"], 782.64)

        result, data = self.call("update_employment", {"employment_id": self.employment.id, "has_bachelor": True},
                                 token=self.staff_token)
        self.assertEqual(data["employment"]["monthly_costs"]["2026-03"]["planned"], 913.08)

    def test_set_monthly_salaries(self):
        EmploymentSalaries.objects.create(employment=self.employment, salary=Decimal("5000.00"),
                                          start_date=date(2026, 1, 1), end_date=date(2027, 12, 31))
        arguments = {"employment_id": self.employment.id, "salaries": {"2026-06": 4200, "2026-07": "4300.5"}}

        result, data = self.call("set_monthly_salaries", {**arguments, "dry_run": True}, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual(data["employment"]["monthly_costs"]["2026-06"]["planned"], 4200.0)
        self.assertEqual(data["cost_changes"][0]["change"], -1499.5)
        self.assertEqual(EmploymentSalaries.objects.filter(employment=self.employment).count(), 1)

        result, data = self.call("set_monthly_salaries", arguments, token=self.staff_token)
        self.assertFalse(result["isError"], data)
        costs = data["employment"]["monthly_costs"]
        self.assertEqual((costs["2026-05"]["planned"], costs["2026-06"]["planned"], costs["2026-07"]["planned"],
                          costs["2026-08"]["planned"]), (5000.0, 4200.0, 4300.5, 5000.0))

        for salaries, expected in (({"2025-12": 1}, "außerhalb"), ({"2026-13": 1}, "JJJJ-MM"),
                                   ({"2026-06": -1}, "negativ"), ({"2026-06": "x"}, "Betrag"), ({}, "salaries")):
            result, message = self.call("set_monthly_salaries", {"employment_id": self.employment.id,
                                                                 "salaries": salaries}, token=self.staff_token)
            self.assertTrue(result["isError"], salaries)
            self.assertIn(expected, message, salaries)

    def test_update_employment_reestimate_without_changes(self):
        result, message = self.call("update_employment", {"employment_id": self.employment.id},
                                    token=self.staff_token)
        self.assertTrue(result["isError"])
        self.add_student_rates()
        self.employment.category = "student"
        self.employment.percentage = Decimal("25")
        self.employment.save()

        result, data = self.call("update_employment", {"employment_id": self.employment.id, "reestimate": True},
                                 token=self.staff_token)

        self.assertFalse(result["isError"], data)
        self.assertTrue(data["salaries_estimated"])
        self.assertEqual(data["employment"]["monthly_costs"]["2026-03"]["planned"], 782.64)

    def test_apply_sap_salaries(self):
        EmploymentSalaries.objects.create(employment=self.employment, salary=Decimal("0.00"),
                                          start_date=date(2026, 1, 1), end_date=date(2027, 12, 31))
        self.add_sap_actuals(["2026-01", "2026-02"])

        result, data = self.call("apply_sap_salaries", {"employment_id": self.employment.id, "dry_run": True},
                                 token=self.staff_token)
        self.assertFalse(result["isError"], data)
        self.assertEqual(data["months_applied"], ["2026-01", "2026-02"])
        self.assertEqual(data["cost_changes"][0]["change"], 10000.0)
        self.assertEqual(self.call("get_staff_member", {"staff_id": self.member.id})[1]
                         ["employments"][0]["monthly_costs"]["2026-01"]["planned"], 0.0)

        result, data = self.call("apply_sap_salaries", {"employment_id": self.employment.id}, token=self.staff_token)
        costs = data["employment"]["monthly_costs"]
        self.assertEqual((costs["2026-01"]["planned"], costs["2026-02"]["planned"], costs["2026-03"]["planned"]),
                         (5000.0, 5000.0, 0.0))

        result, _ = self.call("apply_sap_salaries", {"employment_id": self.employment.id})
        self.assertTrue(result["isError"])

    def test_apply_sap_salaries_without_link(self):
        result, message = self.call("apply_sap_salaries", {"employment_id": self.employment.id},
                                    token=self.staff_token)
        self.assertTrue(result["isError"])
        self.assertIn("SAP-Position", message)

    def test_set_monthly_salaries_keeps_sap_months(self):
        self.add_sap_actuals(["2026-01"])

        result, message = self.call("set_monthly_salaries", {
            "employment_id": self.employment.id, "salaries": {"2026-01": 100, "2026-02": 100},
        }, token=self.staff_token)

        self.assertTrue(result["isError"])
        self.assertIn("2026-01", message)
        self.assertFalse(EmploymentSalaries.objects.filter(employment=self.employment).exists())
        result, _ = self.call("set_monthly_salaries", {"employment_id": self.employment.id,
                                                       "salaries": {"2026-02": 100}})
        self.assertTrue(result["isError"])
