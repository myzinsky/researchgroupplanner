import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from mcp_api.models import ApiToken
from projects.models import Project, ProjectDeliverable, StaffBudgetItem
from staffing.models import Employment, Rebooking, StaffFundingAllocation, StaffMember


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
        employment = Employment.objects.create(staff_member=self.member, start_date=date(2026, 1, 1),
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
