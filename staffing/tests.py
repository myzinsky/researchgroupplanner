from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from projects.models import Project, StaffBudgetItem
from staffing.models import Employment, StaffFundingAllocation, StaffMember


class UniversalStaffFundingTests(TestCase):
    def setUp(self):
        self.staff_member = StaffMember.objects.create(
            first_name="Universal",
            last_name="Person",
        )
        self.employment = Employment.objects.create(
            staff_member=self.staff_member,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            percentage=Decimal("100.00"),
        )

    def test_universal_is_a_valid_exclusive_funding_source(self):
        allocation = StaffFundingAllocation(
            employment=self.employment,
            is_universal=True,
            percentage=Decimal("50.00"),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )

        allocation.full_clean()

        self.assertEqual(allocation.source(), "Universalprojekt")

    def test_allocation_without_a_source_is_invalid(self):
        allocation = StaffFundingAllocation(
            employment=self.employment,
            percentage=Decimal("50.00"),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )

        with self.assertRaisesMessage(ValidationError, "Universalprojekt"):
            allocation.full_clean()

    def test_universal_cannot_be_combined_with_a_project_budget(self):
        project = Project.objects.create(
            acronym="PROJECT",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            budget_total=Decimal("1000.00"),
        )
        budget_item = StaffBudgetItem.objects.create(
            project=project,
            title="Personnel",
            amount=Decimal("1000.00"),
        )
        allocation = StaffFundingAllocation(
            employment=self.employment,
            budget_item=budget_item,
            is_universal=True,
            percentage=Decimal("50.00"),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )

        with self.assertRaisesMessage(ValidationError, "genau eine"):
            allocation.full_clean()

    def test_staff_details_display_universal_funding(self):
        StaffFundingAllocation.objects.create(
            employment=self.employment,
            is_universal=True,
            percentage=Decimal("50.00"),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        user = get_user_model().objects.create_user(
            username="universal-user",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("staffing:details", args=[self.staff_member.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Universalprojekt")

    def test_main_timeline_displays_universal_funding(self):
        StaffFundingAllocation.objects.create(
            employment=self.employment,
            is_universal=True,
            percentage=Decimal("50.00"),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        user = get_user_model().objects.create_user(
            username="universal-timeline-user",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Universalprojekt")
