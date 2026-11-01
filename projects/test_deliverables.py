from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from projects.models import Project, ProjectDeliverable


STATIC_STORAGE = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=STATIC_STORAGE)
class DeliverableTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("user", password="x"))
        today = timezone.now().date()
        self.project = Project.objects.create(
            acronym="DLV", start_date=today - timedelta(days=365), end_date=today + timedelta(days=365),
            budget_total=Decimal("1000"),
        )
        self.soon = ProjectDeliverable.objects.create(
            project=self.project, title="D1.1 Spezifikation", deadline=today + timedelta(days=10), is_lead=True,
        )
        ProjectDeliverable.objects.create(project=self.project, title="D2.1 Bericht", deadline=today + timedelta(days=200))

    def test_details_list_deliverables(self):
        response = self.client.get(reverse("projects:details", args=["DLV"]))

        self.assertEqual([d.title for d in response.context["deliverables"]], ["D1.1 Spezifikation", "D2.1 Bericht"])
        self.assertContains(response, "in ≤ 30 Tagen")
        self.assertContains(response, '<span class="badge text-bg-primary">Federführung</span>')

    def test_main_timeline_shows_deliverables(self):
        response = self.client.get(reverse("main"))

        self.assertContains(response, "Deliverable: D1.1 Spezifikation")
        self.assertContains(response, "deliverable-point deliverable-lead")

    def test_no_section_without_deliverables(self):
        ProjectDeliverable.objects.all().delete()

        response = self.client.get(reverse("projects:details", args=["DLV"]))

        self.assertNotContains(response, "<h2>Deliverables</h2>")

    def test_links_sorted_by_title_in_overview(self):
        from projects.models import ProjectLink

        ProjectLink.objects.create(project=self.project, title="Wiki", url="https://wiki.example.org/dlv")
        ProjectLink.objects.create(project=self.project, title="Ablage", url="https://cloud.example.org/dlv")

        response = self.client.get(reverse("projects:details", args=["DLV"]))

        self.assertEqual([link.title for link in response.context["links"]], ["Ablage", "Wiki"])
        self.assertContains(response, '<a href="https://cloud.example.org/dlv" target="_blank" rel="noopener noreferrer">Ablage</a>')

    def test_contacts_sorted_by_function(self):
        from projects.models import ProjectContact

        ProjectContact.objects.create(project=self.project, name="Zoe Zander", function="Projektträger", email="zoe@example.org")
        ProjectContact.objects.create(project=self.project, name="Anna Arndt", function="Koordination")

        response = self.client.get(reverse("projects:details", args=["DLV"]))

        self.assertEqual([c.function for c in response.context["contacts"]], ["Koordination", "Projektträger"])
        self.assertContains(response, '<a href="mailto:zoe@example.org">zoe@example.org</a>')

    def test_main_page_shows_project_on_its_last_day(self):
        today = timezone.now().date()
        Project.objects.create(acronym="LASTDAY", start_date=today - timedelta(days=100),
                               end_date=today - timedelta(days=50), extension_planning_date=today,
                               budget_total=Decimal("1000"))

        response = self.client.get(reverse("main"))

        self.assertIn("LASTDAY", [p.acronym for p in response.context["projects"]])

    def test_overview_links_sap_fund(self):
        from projects.models import SAPFund
        from sap_integration.models import SAPImport

        fund = SAPFund.objects.create(fund_number="1/070009900", project=self.project)
        with self.settings(SAP_ENABLED=False, SAP_GM_IMPORT_ENABLED=True):
            response = self.client.get(reverse("projects:details", args=["DLV"]))
            self.assertContains(response, "1/070009900")
            self.assertEqual(response.context["sap_funds"][0]["url"], None)

            SAPImport.objects.create(fund=fund, file_name="export.xlsx")
            response = self.client.get(reverse("projects:details", args=["DLV"]))
            self.assertEqual(response.context["sap_funds"][0]["url"],
                             reverse("sap_integration:reconciliation", args=[fund.id]))
