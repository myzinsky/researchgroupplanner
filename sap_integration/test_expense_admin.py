from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from projects.models import OtherBudgetItem, OtherBudgetItemTransaction, Project, SAPFund
from sap_integration.expense_unlink import unlink_expenses, unlink_preview
from sap_integration.models import SAPExpenseAudit, SAPExpenseLink, SAPExpenseUnlinkArchive


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class SAPExpenseLinkAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("expense-admin", password="test")
        cls.staff = get_user_model().objects.create_user("expense-viewer", is_staff=True)
        project = Project.objects.create(
            acronym="ADMIN-TEST", start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31), budget_total=10000,
        )
        cls.fund = SAPFund.objects.create(fund_number="ADMIN-FUND", project=project)
        cls.budget = OtherBudgetItem.objects.create(project=project, title="Reisebudget", amount=3000)
        cls.booking = OtherBudgetItemTransaction.objects.create(
            budget_item=cls.budget, date=date(2026, 3, 12), amount=Decimal("141.50"),
            description="Konferenzreise", sap_id="WUE:2026:ADMIN-FUND:FI-000141",
        )
        cls.link = SAPExpenseLink.objects.create(
            fund=cls.fund, year=2026, source_key="test-source", source_type="actual",
            planner_transaction=cls.booking, confirmed_by=cls.admin,
            source_snapshot={"rows": [{"document_number": "FI-000141"}]},
            planning_snapshot={"main": {"amount": "141.50"}},
        )
        cls.audit = SAPExpenseAudit.objects.create(
            link=cls.link, user=cls.admin, action="apply", after={"note": "AUDIT-MARKER"},
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.list_url = reverse("admin:sap_integration_sapexpenselink_changelist")
        self.detail_url = reverse("admin:sap_integration_sapexpenselink_change", args=[self.link.pk])

    def test_admin_menu_and_searchable_filtered_list(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "SAP-Ausgabenverknüpfungen")
        self.assertContains(response, self.list_url)
        for query in ("ADMIN-TEST", "Reisebudget", "FI-000141", str(self.link.pk)):
            with self.subTest(query=query):
                response = self.client.get(self.list_url, {
                    "q": query, "year": 2026, "fund__id__exact": self.fund.pk,
                    "source_type__exact": "actual", "is_active__exact": "1",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(list(response.context["cl"].queryset), [self.link])
                self.assertContains(response, "Reisebudget")
                self.assertContains(response, self.detail_url)
        response = self.client.get(self.list_url, {"is_active__exact": "0"})
        self.assertEqual(list(response.context["cl"].queryset), [])

    def test_detail_shows_budget_link_snapshots_and_readonly_audit(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin:projects_otherbudgetitem_change", args=[self.budget.pk]))
        self.assertContains(response, "FI-000141")
        self.assertContains(response, "Übernahmeprotokolle")
        self.assertContains(response, "AUDIT-MARKER")
        self.assertNotContains(response, 'name="_save"')
        self.assertNotContains(response, 'name="planner_transaction"')
        self.assertNotContains(response, 'name="audit_entries-0-DELETE"')
        self.assertFalse(response.context["has_change_permission"])
        self.assertFalse(response.context["has_delete_permission"])

    def test_direct_mutation_requests_are_denied_even_for_superuser(self):
        add_url = reverse("admin:sap_integration_sapexpenselink_add")
        delete_url = reverse("admin:sap_integration_sapexpenselink_delete", args=[self.link.pk])
        for method, url, data in (
            ("get", add_url, {}), ("post", add_url, {}),
            ("get", delete_url, {}), ("post", delete_url, {"post": "yes"}),
            ("post", self.detail_url, {"is_active": "", "source_key": "tampered"}),
        ):
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url, data).status_code, 403)
        self.link.refresh_from_db()
        self.assertTrue(self.link.is_active)
        self.assertEqual(self.link.source_key, "test-source")
        self.assertTrue(SAPExpenseAudit.objects.filter(pk=self.audit.pk).exists())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.amount, Decimal("141.50"))

    def test_staff_requires_model_view_permission(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        self.assertEqual(self.client.get(self.detail_url).status_code, 403)
        self.staff.user_permissions.add(Permission.objects.get(codename="view_sapexpenselink"))
        self.assertEqual(self.client.get(self.list_url).status_code, 200)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "AUDIT-MARKER")
        self.staff.user_permissions.add(Permission.objects.get(codename="view_sapexpenseaudit"))
        self.assertContains(self.client.get(self.detail_url), "AUDIT-MARKER")

    def unlink_confirmation(self):
        response = self.client.post(self.list_url, {
            "action": "unlink_selected", "_selected_action": [self.link.pk],
        })
        self.assertEqual(response.status_code, 200)
        return response

    def confirm_unlink(self, token):
        return self.client.post(self.list_url, {
            "action": "unlink_selected", "_selected_action": [self.link.pk],
            "confirm_unlink": "yes", "unlink_state": token,
        })

    def test_unlink_requires_confirmation_and_archives_history(self):
        response = self.unlink_confirmation()
        self.assertContains(response, "Die Planungseinträge mit ihren Beträgen bleiben bestehen.")
        self.assertContains(response, "erneute Sammelübernahme")
        self.assertTrue(SAPExpenseLink.objects.filter(pk=self.link.pk).exists())
        self.assertFalse(SAPExpenseUnlinkArchive.objects.exists())
        response = self.confirm_unlink(response.context["unlink_state"])
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SAPExpenseLink.objects.filter(pk=self.link.pk).exists())
        self.assertFalse(SAPExpenseAudit.objects.filter(pk=self.audit.pk).exists())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.sap_id, "")
        self.assertEqual(self.booking.amount, Decimal("141.50"))
        self.assertEqual(self.booking.description, "Konferenzreise")
        archive = SAPExpenseUnlinkArchive.objects.get(original_link_id=self.link.pk)
        self.assertEqual(archive.created_by, self.admin)
        self.assertEqual(archive.fund_number, "ADMIN-FUND")
        archived = {row["model"]: row for row in archive.snapshot["records"]}
        self.assertEqual(archived["sap_integration.sapexpenseaudit"]["fields"]["after"], {"note": "AUDIT-MARKER"})
        self.assertEqual(archived["projects.otherbudgetitemtransaction"]["fields"]["sap_id"], "WUE:2026:ADMIN-FUND:FI-000141")
        self.assertEqual(archive.snapshot["cleared_reference_ids"], [self.booking.pk])
        # Archives never retain protected foreign keys to disposable planning.
        self.budget.delete()
        self.assertTrue(SAPExpenseUnlinkArchive.objects.filter(pk=archive.pk).exists())
        archive_url = reverse("admin:sap_integration_sapexpenseunlinkarchive_change", args=[archive.pk])
        self.assertContains(self.client.get(archive_url), "AUDIT-MARKER")

    def test_tampered_and_stale_confirmation_cannot_unlink(self):
        preview = self.unlink_confirmation()
        token = preview.context["unlink_state"]
        self.assertEqual(self.confirm_unlink(token + "invalid").status_code, 302)
        self.assertTrue(SAPExpenseLink.objects.filter(pk=self.link.pk).exists())
        self.booking.amount = Decimal("999.00")
        self.booking.save()
        self.assertEqual(self.confirm_unlink(token).status_code, 302)
        self.assertTrue(SAPExpenseLink.objects.filter(pk=self.link.pk).exists())
        self.assertFalse(SAPExpenseUnlinkArchive.objects.exists())

    def test_view_only_staff_cannot_unlink_even_with_forged_action(self):
        token = self.unlink_confirmation().context["unlink_state"]
        self.staff.user_permissions.add(Permission.objects.get(codename="view_sapexpenselink"))
        self.client.force_login(self.staff)
        response = self.client.get(self.list_url)
        self.assertNotContains(response, 'value="unlink_selected"')
        self.confirm_unlink(token)
        self.assertTrue(SAPExpenseLink.objects.filter(pk=self.link.pk).exists())
        self.assertFalse(SAPExpenseUnlinkArchive.objects.exists())

    def test_unlink_leaves_other_links_and_shared_reference_intact(self):
        other = SAPExpenseLink.objects.create(
            fund=self.fund, year=2026, source_key="superseded-commitment", source_type="commitment",
            planner_transaction=self.booking, is_active=False,
        )
        token = self.unlink_confirmation().context["unlink_state"]
        self.confirm_unlink(token)
        self.assertTrue(SAPExpenseLink.objects.filter(pk=other.pk).exists())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.sap_id, "WUE:2026:ADMIN-FUND:FI-000141")

    def test_unlink_handles_remainder_and_preserves_foreign_sap_reference(self):
        remainder = OtherBudgetItemTransaction.objects.create(
            budget_item=self.budget, date=date(2026, 3, 1), amount=10,
            sap_id="WUE:2026:ADMIN-FUND:OLD-OBLIGO",
        )
        self.link.remainder_transaction = remainder
        self.link.save()
        self.booking.sap_id = "GM:MANUAL-REFERENCE"
        self.booking.save()
        _, _, fingerprint = unlink_preview([self.link.pk])
        unlink_expenses([self.link.pk], expected_state=fingerprint, user=self.admin)
        remainder.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual(remainder.sap_id, "")
        self.assertEqual(self.booking.sap_id, "GM:MANUAL-REFERENCE")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 2)

    def test_archive_failure_rolls_back_link_audit_and_reference_changes(self):
        _, _, fingerprint = unlink_preview([self.link.pk])
        with patch("sap_integration.expense_unlink.SAPExpenseUnlinkArchive.objects.create", side_effect=RuntimeError("archive failed")):
            with self.assertRaises(RuntimeError):
                unlink_expenses([self.link.pk], expected_state=fingerprint, user=self.admin)
        self.assertTrue(SAPExpenseLink.objects.filter(pk=self.link.pk).exists())
        self.assertTrue(SAPExpenseAudit.objects.filter(pk=self.audit.pk).exists())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.sap_id, "WUE:2026:ADMIN-FUND:FI-000141")

    def test_archive_is_readonly_even_for_superuser(self):
        _, _, fingerprint = unlink_preview([self.link.pk])
        unlink_expenses([self.link.pk], expected_state=fingerprint, user=self.admin)
        archive = SAPExpenseUnlinkArchive.objects.get()
        for url in (
            reverse("admin:sap_integration_sapexpenseunlinkarchive_add"),
            reverse("admin:sap_integration_sapexpenseunlinkarchive_change", args=[archive.pk]),
            reverse("admin:sap_integration_sapexpenseunlinkarchive_delete", args=[archive.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.assertTrue(SAPExpenseUnlinkArchive.objects.filter(pk=archive.pk).exists())
