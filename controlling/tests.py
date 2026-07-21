from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from projects.models import Project, StaffBudgetItem, StaffBudgetItemEligibility
from staffing.models import Employment, EmploymentSalaries, StaffFundingAllocation, StaffMember


class BudgetWarningTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="warning-user",
            password="test-password",
        )
        self.client.force_login(user)

    def test_staff_budget_overrun_shows_difference(self):
        project = Project.objects.create(
            acronym="DIFF",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            budget_total=Decimal("100.00"),
            no_overhead=True,
        )
        budget_item = StaffBudgetItem.objects.create(
            project=project,
            title="Personnel",
            amount=Decimal("100.00"),
        )
        StaffBudgetItemEligibility.objects.create(
            budget_item=budget_item,
            eligible_employment="researcher",
        )
        staff_member = StaffMember.objects.create(
            first_name="Test",
            last_name="Person",
        )
        employment = Employment.objects.create(
            staff_member=staff_member,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            percentage=Decimal("100.00"),
        )
        EmploymentSalaries.objects.create(
            employment=employment,
            salary=Decimal("110.00"),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
        StaffFundingAllocation.objects.create(
            employment=employment,
            budget_item=budget_item,
            percentage=Decimal("100.00"),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )

        response = self.client.get(reverse("warnings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Differenz: 10.00 EUR")


class StaffStatusWarningTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="staff-warning-user",
            password="test-password",
        )
        self.client.force_login(user)

    def test_leadership_without_employment_has_no_status_warning(self):
        staff_member = StaffMember.objects.create(
            first_name="Leadership",
            last_name="Person",
            is_leadership=True,
            status="active",
        )

        response = self.client.get(reverse("warnings"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response,
            f"Status-Inkonsistenz bei {staff_member}",
        )

    def test_non_leadership_without_employment_keeps_status_warning(self):
        staff_member = StaffMember.objects.create(
            first_name="Regular",
            last_name="Person",
            is_leadership=False,
            status="active",
        )

        response = self.client.get(reverse("warnings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"Status-Inkonsistenz bei {staff_member}",
        )
