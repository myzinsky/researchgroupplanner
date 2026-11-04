import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from projects.models import OtherBudgetItem, OtherBudgetItemTransaction, Project, SAPFund
from sap_integration.cache import load_year
from sap_integration.category_rules import classify, digest
from sap_integration.expense_config import FORMAT, apply_config, export_config, preview_config, read_config, load_local_config, local_config_path
from sap_integration.expenses import apply_expense, apply_expenses, build_expense_checks, bulk_expense_state, expense_warnings, preview_expense
from sap_integration.forms import ExpenseApplyForm, ExpenseCategoryForm
from sap_integration.models import SAPExpenseAudit, SAPExpenseBudgetMapping, SAPExpenseCategory, SAPExpenseLink
from sap_integration.parser import DOCUMENT_COLUMNS, parse_downloaded_reports
from sap_integration.tests import _write_workbook


STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def example_personnel_categories():
    return [
        {"key": "staff_doctoral", "label": "Doktoranden E13", "kind": "staff", "exact_codes": ["42900-0201", "42901-0201"], "patterns": [], "is_active": True},
        {"key": "staff_assistants", "label": "Hilfskräfte", "kind": "staff", "exact_codes": ["42900-0202", "42902-0202"], "patterns": [], "is_active": True},
        {"key": "staff_civil_servants_old", "label": "Personal (alte Sammelposition)", "kind": "staff", "exact_codes": [], "patterns": ["*-0901"], "is_active": True},
    ]


def example_categories():
    """Entirely synthetic configuration; no institution's financial code list."""
    return [
        {"key": "travel", "label": "Reisekosten", "kind": "travel", "exact_codes": ["88000-0101", "88001-0101"], "patterns": ["*-0101"], "is_active": True},
        {"key": "consumables", "label": "Verbrauchsmaterial", "kind": "other", "exact_codes": ["88000-0102", "88001-0102", "88002-0102"], "patterns": [], "is_active": True},
        {"key": "administration_old", "label": "Verwaltung (alte Sammelposition)", "kind": "other", "exact_codes": [], "patterns": ["*-0902"], "is_active": True},
        {"key": "materials_old", "label": "Material (alte Sammelposition)", "kind": "other", "exact_codes": [], "patterns": ["*-0903"], "is_active": True},
        {"key": "events_old", "label": "Veranstaltungen (alte Sammelposition)", "kind": "other", "exact_codes": [], "patterns": ["*-0904"], "is_active": True},
    ] + example_personnel_categories()


def booking(**changes):
    return {"type": "actual", "financial_center": "1000", "financial_position": "88001-0101",
            "gl_account": "6800", "document_number": "123456", "document_type": "KR",
            "document_item": "0", "purchase_document": "", "reference": "RE-001",
            "reversal_document": "", "header_text": "Reise", "item_text": "Berlin",
            "position": "Reise Berlin", "business_partner": "Reisebüro", "amount": "400.00",
            "booking_date": "2026-03-12", "source_row": 2, **changes}


class ExpenseParserTests(SimpleTestCase):
    def test_reparse_preserves_documents_dates_signs_and_duplicate_source_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw" / "2026"
            raw.mkdir(parents=True)
            _write_workbook(raw / "budget.xlsx", ["Fonds", "Betrag"], [["F", 1000]])
            headers = ["Fonds", "Name des Geschäftspartners", "Betrag", "Buchungsdatum", *DOCUMENT_COLUMNS.values()]
            values = ["F", "Partner", -25, date(2026, 3, 12), *[
                {"Finanzstelle": "1000", "Finanzposition": "88000-0101", "Sachkonto": "6800",
                 "FI-Belegnummer": "000123", "Belegart": "KR", "Stornobeleg": "000124",
                 "Referenz": "R1", "Belegkopftext": "Kopf", "Positionstext": "Text",
                 "Einkaufsbeleg": "000009", "Position": 0}[column] for column in DOCUMENT_COLUMNS.values()
            ]]
            _write_workbook(raw / "actual.xlsx", headers, [values, values])
            _write_workbook(raw / "commitments.xlsx", headers, [])
            parse_downloaded_reports(directory, 2026, ["F"])
            payload = load_year(directory, 2026)
            rows = payload["funds"]["F"]["transactions"]
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(len(rows), 2)
            self.assertEqual([r["source_row"] for r in rows], [2, 3])
            self.assertEqual(rows[0]["document_number"], "000123")
            self.assertEqual(rows[0]["document_item"], "0")
            self.assertEqual(rows[0]["reversal_document"], "000124")
            self.assertEqual(rows[0]["financial_position"], "88000-0101")
            self.assertEqual(rows[0]["amount"], "-25.00")
            self.assertEqual(rows[0]["booking_date"], "2026-03-12")
            self.assertEqual(rows[0]["position"], "Kopf Text")


@override_settings(SAP_ENABLED=True, SAP_GM_IMPORT_ENABLED=False, STORAGES=STORAGES)
class ExpenseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("sap-reviewer", password="x", is_staff=True)
        cls.project = Project.objects.create(acronym="TEST", start_date=date(2024, 1, 1), end_date=date(2027, 12, 31), budget_total=10000)
        cls.fund = SAPFund.objects.create(fund_number="TEST-FUND", project=cls.project)
        cls.budget = OtherBudgetItem.objects.create(project=cls.project, title="Reisen", amount=3000)
        cls.category = SAPExpenseCategory.objects.create(key="travel", label="Reisekosten", kind="travel",
                                                       exact_codes=["88000-0101", "88001-0101"], patterns=["*-0101"])
        SAPExpenseBudgetMapping.objects.create(fund=cls.fund, category=cls.category, budget_item=cls.budget)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        override = override_settings(SAP_DATA_DIR=self.directory)
        override.enable()
        self.addCleanup(override.disable)
        config_path = local_config_path()
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"format": FORMAT, "version": 1, "categories": example_categories()}))
        self.write([booking()])
        self.client.force_login(self.user)

    def write(self, rows, version=2):
        folder = self.directory / "processed"
        folder.mkdir(exist_ok=True)
        values = {"transactions": rows, "budget": "3000.00", "has_budget": True,
                  "actual_total": str(sum(Decimal(r["amount"]) for r in rows if r["type"] == "actual")),
                  "commitments_total": str(sum(Decimal(r["amount"]) for r in rows if r["type"] == "commitment")),
                  "combined_total": "0.00", "remaining": "0.00"}
        (folder / "2026.json").write_text(json.dumps({"schema_version": version, "year": 2026,
                                                     "funds": {self.fund.fund_number: values}}))

    def checks(self):
        return build_expense_checks(self.directory, self.fund, 2026)

    def check(self, document="123456"):
        return next(c for c in self.checks() if c.row.get("document_number") == document and not c.missing)

    def data(self, **changes):
        raw = {"budget_item": self.budget.pk, **changes}
        form = ExpenseApplyForm(raw, project=self.project)
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data

    def apply(self, check=None, **changes):
        check = check or self.check()
        data = self.data(**changes)
        return apply_expense(self.directory, self.fund, 2026, check.key, data,
                             expected_preview=preview_expense(check, data), user=self.user)

    def planned(self, amount="1000.00", **changes):
        return OtherBudgetItemTransaction.objects.create(budget_item=self.budget, amount=amount,
                                                        date=date(2026, 4, 1), description="Geplante Reise", **changes)

    def test_old_list_wildcards_and_exact_priority(self):
        other = SAPExpenseCategory.objects.create(key="other", label="Allgemein", kind="other", patterns=["8800?-*"])
        self.assertEqual(classify("88001-0101", [self.category, other])[0], self.category)
        self.assertEqual(classify("99-123-0101", [self.category, other])[0], self.category)
        self.assertEqual(classify("98-0101", [self.category, other])[0], self.category)
        other.exact_codes = ["88001-0101"]
        other.save()
        self.assertFalse(self.check().can_apply)
        self.assertIn("Mehrere Kategorien", self.check().blocking[0])

    def test_pattern_conflicts_are_blocked(self):
        SAPExpenseCategory.objects.create(key="other", label="Andere Reise", kind="other", patterns=["99-*"])
        self.write([booking(financial_position="99-123-0101")])
        self.assertFalse(self.check().can_apply)

    def test_rule_editor_validates_number_lists(self):
        form = ExpenseCategoryForm({"key": "new", "label": "Neu", "kind": "other", "exact_codes": "123-45, 123-46\n123-47",
                                    "patterns": "99-*-0101", "is_active": True})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["exact_codes"], ["123-45", "123-46", "123-47"])
        form = ExpenseCategoryForm({"key": "new", "label": "Neu", "kind": "other", "exact_codes": "123-*"})
        self.assertFalse(form.is_valid())

    def test_duplicate_components_are_blocked_and_never_dropped(self):
        self.write([booking(), booking(source_row=10, amount="250.00", booking_date="2026-03-14")])
        check = self.check()
        self.assertEqual(len(check.rows), 2)
        self.assertFalse(check.can_apply)
        with self.assertRaises(ValidationError):
            self.apply(check)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)

    def test_same_amount_different_documents_stay_separate(self):
        self.write([booking(), booking(document_number="999", amount="-400.00")])
        self.assertEqual(len(self.checks()), 2)
        self.assertEqual({c.amount for c in self.checks()}, {Decimal("400.00"), Decimal("-400.00")})

    def test_personnel_and_income_stay_out_of_expense_flow(self):
        self.write([booking(financial_position="42900-0201"), booking(document_number="2", position="Gehalt März 2026"),
                    booking(document_number="3", financial_position="28000-0301")])
        self.assertEqual(self.checks(), [])

    def test_old_cache_still_loads_but_expenses_request_offline_reparse(self):
        self.write([booking()], version=1)
        self.assertEqual(load_year(self.directory, 2026)["schema_version"], 1)
        with self.assertRaisesMessage(ValidationError, "parse_sap"):
            self.checks()

    def test_create_copies_source_values_and_has_audit(self):
        link = self.apply()
        target = link.planner_transaction
        self.assertEqual(target.amount, Decimal("400.00"))
        self.assertEqual(target.date, date(2026, 3, 12))
        self.assertEqual(target.description, "Reise Berlin")
        self.assertIn("WUE:2026:TEST-FUND:123456", target.sap_id)
        self.assertEqual(link.audit_entries.count(), 1)
        self.assertEqual(self.check().status, "Übereinstimmend")

    def test_repeated_submit_cannot_create_duplicate(self):
        check = self.check()
        data = self.data()
        preview = preview_expense(check, data)
        apply_expense(self.directory, self.fund, 2026, check.key, data, expected_preview=preview, user=self.user)
        with self.assertRaises(ValidationError):
            apply_expense(self.directory, self.fund, 2026, check.key, data, expected_preview=preview, user=self.user)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)
        self.assertEqual(SAPExpenseLink.objects.count(), 1)

    def test_reparse_order_amount_description_and_date_preserve_link(self):
        link = self.apply()
        self.write([booking(source_row=99, amount="450.00", position="Reise Hamburg", booking_date="2026-03-20")])
        check = self.check()
        self.assertEqual(check.link.pk, link.pk)
        self.assertEqual(check.status, "Abweichung")
        self.apply(check, planner_transaction=link.planner_transaction_id)
        link.planner_transaction.refresh_from_db()
        self.assertEqual(link.planner_transaction.amount, Decimal("450.00"))
        self.assertEqual(link.planner_transaction.date, date(2026, 3, 20))
        self.assertEqual(link.planner_transaction.description, "Reise Hamburg")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)

    def test_existing_plan_keeps_its_budget_and_needs_no_budget_selection(self):
        target = self.planned()
        other_budget = OtherBudgetItem.objects.create(project=self.project, title="Anderes Sachmittelbudget", amount=2000)
        target.budget_item = other_budget
        target.save()
        data = self.data(planner_transaction=target.pk, budget_item="")
        self.assertEqual(data["budget_item"], other_budget)
        self.apply(planner_transaction=target.pk)
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("400.00"))
        self.assertEqual(target.budget_item, other_budget)
        self.assertEqual(target.description, "Reise Berlin")
        self.assertEqual(target.sap_id, "WUE:2026:TEST-FUND:123456")

    def test_wien_plan_is_updated_from_1000_to_850_without_double_charge(self):
        target = self.planned()
        self.write([booking(amount="850.00", position="Reise Wien")])
        preview = preview_expense(self.check(), self.data(planner_transaction=target.pk))
        self.assertEqual(Decimal(preview["delta"]), Decimal("-150"))
        self.apply(planner_transaction=target.pk)
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("850.00"))
        self.assertEqual(target.date, date(2026, 3, 12))
        self.assertEqual(target.description, "Reise Wien")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)
        self.assertEqual(sum(t.amount for t in OtherBudgetItemTransaction.objects.all()), Decimal("850.00"))

    def test_removed_form_options_cannot_create_additional_costs_or_skip_update(self):
        target = self.planned()
        link = self.apply(planner_transaction=target.pk, remaining_amount="600", remaining_date="2026-05-01", link_only=True)
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("400.00"))
        self.assertIsNone(link.remainder_transaction)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)

    def test_selecting_commitment_plan_updates_to_actual_and_suppresses_old_source(self):
        self.write([booking(type="commitment", amount="1000.00")])
        old = self.apply()
        self.write([booking(type="commitment", amount="1000.00"), booking(document_number="PAID", amount="900.00")])
        new = self.apply(self.check("PAID"), planner_transaction=old.planner_transaction_id)
        old.refresh_from_db()
        self.assertFalse(old.is_active)
        self.assertTrue(new.is_active)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)
        self.assertEqual(new.planner_transaction.amount, Decimal("900"))
        self.assertEqual(next(c for c in self.checks() if c.link.pk == old.pk).status, "Abgelöstes Obligo")
        response = self.client.get(reverse("sap_integration:expenses", args=[self.fund.pk]))
        category_summary = next(row for row in response.context["category_rows"] if row["category"] == self.category)
        self.assertEqual(category_summary["commitment"], Decimal("1000.00"))
        self.assertEqual(category_summary["actual"], Decimal("900.00"))

    def test_credit_preserves_original_cost_and_adds_signed_correction(self):
        original = self.apply()
        self.write([booking(), booking(document_number="REVERSAL", amount="-400.00", reversal_document="123456")])
        credit = self.check("REVERSAL")
        with self.assertRaises(ValidationError):
            self.apply(credit, planner_transaction=original.planner_transaction_id)
        self.apply(credit)
        self.assertEqual(sum(t.amount for t in OtherBudgetItemTransaction.objects.all()), Decimal("0.00"))
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 2)

    def test_gm_references_are_never_overwritten(self):
        target = self.planned(sap_id="GM-123")
        with self.assertRaisesMessage(ValidationError, "GM"):
            self.apply(planner_transaction=target.pk)
        target.refresh_from_db()
        self.assertEqual(target.sap_id, "GM-123")
        self.assertEqual(target.amount, Decimal("1000"))

    def test_missing_source_and_reclassified_source_require_review(self):
        self.apply()
        self.write([booking(financial_position="88000-0101")])
        checks = self.checks()
        self.assertEqual(len(checks), 2)
        self.assertTrue(any(c.missing for c in checks))
        self.assertFalse(any(c.can_apply for c in checks))
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)

    def test_changed_source_after_preview_is_rejected_without_planning_writes(self):
        check = self.check()
        data = self.data()
        preview = preview_expense(check, data)
        self.write([booking(amount="999.00")])
        with self.assertRaisesMessage(ValidationError, "inzwischen"):
            apply_expense(self.directory, self.fund, 2026, check.key, data, expected_preview=preview, user=self.user)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)

    def test_changed_planning_after_preview_is_rejected(self):
        target = self.planned()
        check = self.check()
        data = self.data(planner_transaction=target.pk)
        preview = preview_expense(check, data)
        target.amount = 1234
        target.save()
        with self.assertRaisesMessage(ValidationError, "inzwischen"):
            apply_expense(self.directory, self.fund, 2026, check.key, data, expected_preview=preview, user=self.user)
        self.assertFalse(SAPExpenseLink.objects.exists())

    def test_legacy_additional_planning_is_not_silently_rewritten_or_deleted(self):
        target = self.planned()
        link = self.apply(planner_transaction=target.pk)
        extra = self.planned(amount="600")
        link.remainder_transaction = extra
        link.save()
        self.write([booking(amount="450.00")])
        with self.assertRaisesMessage(ValidationError, "zusätzliche Vorplanung"):
            self.apply(planner_transaction=target.pk)
        target.refresh_from_db()
        extra.refresh_from_db()
        self.assertEqual(target.amount, Decimal("400.00"))
        self.assertEqual(extra.amount, Decimal("600.00"))
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 2)

    def test_later_foreign_sap_reference_is_not_overwritten(self):
        link = self.apply()
        target = link.planner_transaction
        target.sap_id += ", GM-123"
        target.save()
        with self.assertRaisesMessage(ValidationError, "SAP-Referenz"):
            self.apply(planner_transaction=target.pk)

    def test_booking_outside_project_years_cannot_silently_disappear_from_budget(self):
        self.write([booking(booking_date="2030-03-12")])
        self.assertFalse(self.check().can_apply)
        self.assertTrue(any("Projektjahre" in message for message in self.check().blocking))

    def test_cross_project_target_and_mapping_are_rejected(self):
        other = Project.objects.create(acronym="OTHER", start_date=date(2024, 1, 1), end_date=date(2027, 1, 1), budget_total=1000)
        budget = OtherBudgetItem.objects.create(project=other, title="Andere Reisen", amount=1000)
        form = ExpenseApplyForm({"budget_item": budget.pk, "remaining_amount": "0"}, project=self.project)
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValidationError):
            SAPExpenseBudgetMapping(fund=self.fund, category=self.category, budget_item=budget).clean()

    def test_reads_are_read_only_and_one_post_creates_entry_without_duplicates(self):
        detail = reverse("sap_integration:expense_detail", args=[self.fund.pk, 2026, self.check().key])
        for url in [reverse("sap_integration:expense_categories"), reverse("sap_integration:expenses", args=[self.fund.pk]), detail]:
            self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(detail)
        self.assertContains(response, "In Planung übernehmen")
        self.assertNotContains(response, "Änderung bestätigen")
        self.assertFalse(OtherBudgetItemTransaction.objects.exists())
        raw = {"budget_item": self.budget.pk, "action": "apply", "state_token": response.context["state_token"]}
        response = self.client.post(detail, raw)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.assertEqual(self.client.post(detail, raw).status_code, 200)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)

    def test_simple_form_updates_existing_plan_with_no_optional_flags(self):
        target = self.planned()
        self.write([booking(amount="850.00", position="Reise Wien")])
        detail = reverse("sap_integration:expense_detail", args=[self.fund.pk, 2026, self.check().key])
        response = self.client.get(detail)
        for field in ("remaining_amount", "remaining_date", "link_only", "replace_commitment", "confirm_credit"):
            self.assertNotContains(response, f'name="{field}"', html=False)
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("1000.00"))
        raw = {"planner_transaction": target.pk, "action": "apply", "state_token": response.context["state_token"]}
        response = self.client.post(detail, raw)
        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("850.00"))
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)

    def test_invalid_form_state_does_not_write(self):
        detail = reverse("sap_integration:expense_detail", args=[self.fund.pk, 2026, self.check().key])
        response = self.client.post(detail, {"budget_item": self.budget.pk,
                                            "action": "apply", "state_token": "tampered"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(OtherBudgetItemTransaction.objects.exists())

    def test_changed_source_after_page_load_does_not_apply_unseen_amount(self):
        target = self.planned()
        detail = reverse("sap_integration:expense_detail", args=[self.fund.pk, 2026, self.check().key])
        token = self.client.get(detail).context["state_token"]
        self.write([booking(amount="999.00")])
        response = self.client.post(detail, {"planner_transaction": target.pk, "action": "apply", "state_token": token})
        self.assertContains(response, "inzwischen geändert")
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("1000"))
        self.assertFalse(SAPExpenseLink.objects.exists())

    def test_changed_planning_after_page_load_is_not_overwritten(self):
        target = self.planned()
        detail = reverse("sap_integration:expense_detail", args=[self.fund.pk, 2026, self.check().key])
        token = self.client.get(detail).context["state_token"]
        target.amount = 1234
        target.save()
        response = self.client.post(detail, {"planner_transaction": target.pk, "action": "apply", "state_token": token})
        self.assertContains(response, "inzwischen geändert")
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("1234"))
        self.assertFalse(SAPExpenseLink.objects.exists())

    def test_nonstaff_and_disabled_feature_cannot_access_configuration_or_apply(self):
        regular = get_user_model().objects.create_user("regular", password="x")
        self.client.force_login(regular)
        url = reverse("sap_integration:expense_categories")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        with override_settings(SAP_ENABLED=False, SAP_GM_IMPORT_ENABLED=True):
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.get(reverse("sap_integration:expenses", args=[self.fund.pk])).status_code, 404)

    def test_warning_links_to_expense_reconciliation(self):
        warnings = expense_warnings(self.directory)
        self.assertEqual(len(warnings), 1)
        response = self.client.get(reverse("warnings"))
        self.assertContains(response, warnings[0]["link"])
        self.assertContains(response, "SAP-Buchung(en) ohne verknüpften Planungseintrag")
        self.assertContains(response, "Beleg 123456")
        self.assertContains(response, "Nicht in Planung verknüpft")

    def test_warning_opens_affected_year_and_changed_booking_reappears_after_ignoring(self):
        from controlling.models import IgnoredWarning

        path = self.directory / "processed" / "2026.json"
        payload = json.loads(path.read_text())
        payload["year"] = 2025
        payload["funds"][self.fund.fund_number]["transactions"][0]["booking_date"] = "2025-03-12"
        older = path.with_name("2025.json")
        older.write_text(json.dumps(payload))
        self.write([])
        warning = expense_warnings(self.directory)[0]
        self.assertTrue(warning["link"].endswith("?year=2025"))
        first_key = IgnoredWarning.key_for(warning)
        IgnoredWarning.objects.create(key=first_key, comment="Geprüft", created_by=self.user)
        payload["funds"][self.fund.fund_number]["transactions"][0]["document_number"] = "CHANGED"
        older.write_text(json.dumps(payload))
        changed_warning = expense_warnings(self.directory)[0]
        self.assertNotEqual(IgnoredWarning.key_for(changed_warning), first_key)
        response = self.client.get(reverse("warnings"))
        active = [w for w in response.context["warnings_list"] if w["title"] == changed_warning["title"]]
        self.assertEqual(len(active), 1)

    def test_expense_warnings_not_shown_for_other_webgui_backend(self):
        with override_settings(SAP_BACKEND="sap_integration.tests.FakeSAPBackend"):
            response = self.client.get(reverse("warnings"))
        self.assertNotContains(response, "SAP-Ausgabenabgleich:")

    def test_personnel_rules_support_configured_variants_and_broad_headings(self):
        categories = [SAPExpenseCategory(**row) for row in example_categories()]
        for category in categories:
            category.full_clean(validate_unique=False, validate_constraints=False)
        for code, expected in {
            "42900-0201": "staff_doctoral", "42901-0201": "staff_doctoral",
            "42900-0202": "staff_assistants", "42902-0202": "staff_assistants",
            "99-0901": "staff_civil_servants_old", "88002-0102": "consumables",
        }.items():
            category, matches = classify(code, categories)
            self.assertEqual(len(matches), 1, code)
            self.assertEqual(category.key, expected)

    def test_personnel_is_classified_but_cannot_be_imported_as_other_expense(self):
        SAPExpenseCategory.objects.bulk_create([SAPExpenseCategory(**row) for row in example_personnel_categories()])
        self.write([booking(financial_position="42900-0201", position="Personalbuchung")])
        self.assertEqual(self.checks(), [])
        category = SAPExpenseCategory.objects.get(key="staff_doctoral")
        with self.assertRaisesMessage(ValidationError, "Gehaltsabgleich"):
            SAPExpenseBudgetMapping(fund=self.fund, category=category, budget_item=self.budget).clean()
        response = self.client.get(reverse("sap_integration:expense_categories"))
        self.assertContains(response, "Doktoranden E13")
        self.assertContains(response, "Personal · Gehaltsabgleich")
        response = self.client.get(reverse("sap_integration:expenses", args=[self.fund.pk]))
        self.assertFalse(any(row["category"].kind == "staff" for row in response.context["category_rows"]))
        response = self.client.post(reverse("sap_integration:expenses", args=[self.fund.pk]), {
            "action": "map", "category": category.pk, "budget_item": self.budget.pk,
        })
        self.assertEqual(response.status_code, 404)

    def test_personnel_configuration_can_be_exchanged_without_adding_planning(self):
        payload = {"format": FORMAT, "version": 1, "categories": example_personnel_categories()}
        read_config(json.dumps(payload).encode())
        added = apply_config(payload, expected_digest=digest(export_config()), replace_keys=set())
        self.assertEqual(added, len(example_personnel_categories()))
        self.assertFalse(OtherBudgetItemTransaction.objects.exists())
        self.assertEqual(SAPExpenseBudgetMapping.objects.count(), 1)

    def test_planning_page_counts_updated_actual_instead_of_estimate(self):
        target = self.planned()
        self.apply(planner_transaction=target.pk)
        response = self.client.get(reverse("projects:other_budget_items", args=[self.project.acronym]))
        self.assertEqual(response.context["total_used"], Decimal("400"))
        self.assertEqual(response.context["total_actual"], Decimal("400"))
        self.assertContains(response, "WUE:2026:TEST-FUND:123456")

    def test_config_roundtrip_is_portable_and_does_not_contain_project_ids(self):
        payload = export_config()
        self.assertEqual(read_config(json.dumps(payload).encode()), payload)
        self.assertEqual(set(payload["categories"][0]), {"key", "label", "kind", "exact_codes", "patterns", "is_active"})
        self.assertNotIn("budget_item", json.dumps(payload))
        self.assertEqual(apply_config(payload, expected_digest=digest(payload), replace_keys=set()), 0)

    def test_uploaded_config_requires_preview_and_preserves_omitted_categories(self):
        payload = {"format": FORMAT, "version": 1, "categories": [
            {"key": "supplies", "label": "Verbrauchsmaterial", "kind": "other", "exact_codes": ["88001-0102"], "patterns": [], "is_active": True}
        ]}
        url = reverse("sap_integration:expense_categories")
        upload = SimpleUploadedFile("categories.json", json.dumps(payload).encode(), content_type="application/json")
        response = self.client.post(url, {"action": "upload", "config_file": upload})
        self.assertEqual(SAPExpenseCategory.objects.count(), 1)
        response = self.client.post(url, {"action": "import", "preview_token": response.context["preview_token"]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(SAPExpenseCategory.objects.values_list("key", flat=True)), {"travel", "supplies"})
        self.assertEqual(SAPExpenseBudgetMapping.objects.count(), 1)

    def test_historical_broad_categories_are_not_guessed_as_modern_subtypes(self):
        categories = [SAPExpenseCategory(**row) for row in example_categories()]
        self.assertEqual(classify("99-0902", categories)[0].key, "administration_old")
        self.assertEqual(classify("98-0903", categories)[0].key, "materials_old")
        self.assertEqual(classify("97-0904", categories)[0].key, "events_old")

    def test_config_import_conflicts_keep_existing_unless_selected(self):
        payload = export_config()
        payload["categories"][0]["label"] = "Neuer Name"
        baseline = digest(export_config())
        self.assertEqual(preview_config(payload)[0]["status"], "conflict")
        self.assertEqual(apply_config(payload, expected_digest=baseline, replace_keys=set()), 0)
        self.assertEqual(apply_config(payload, expected_digest=baseline, replace_keys={"travel"}), 1)
        self.category.refresh_from_db()
        self.assertEqual(self.category.label, "Neuer Name")
        self.assertEqual(SAPExpenseBudgetMapping.objects.get().category_id, self.category.pk)

    def test_stale_config_and_invalid_payload_are_rejected(self):
        payload = export_config()
        baseline = digest(payload)
        self.category.label = "Geändert"
        self.category.save()
        with self.assertRaises(ValidationError):
            apply_config(payload, expected_digest=baseline, replace_keys={"travel"})
        for invalid in [{}, {"format": FORMAT, "version": 1, "categories": [{}]},
                        {"format": FORMAT, "version": 1, "categories": [dict(payload["categories"][0], exact_codes={})]},
                        {"format": FORMAT, "version": 1, "categories": [dict(payload["categories"][0], exact_codes="*")]}]:
            with self.assertRaises(ValidationError):
                read_config(json.dumps(invalid).encode())

    def test_defaults_use_preview_and_do_not_write_until_confirmed(self):
        url = reverse("sap_integration:expense_categories")
        response = self.client.post(url, {"action": "defaults"})
        self.assertContains(response, "Importvorschau")
        self.assertEqual(SAPExpenseCategory.objects.count(), 1)
        response = self.client.post(url, {"action": "import", "preview_token": response.context["preview_token"]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SAPExpenseCategory.objects.count(), len(example_categories()))
        self.assertFalse(OtherBudgetItemTransaction.objects.exists())
        self.assertEqual(self.client.get(reverse("sap_integration:expense_categories_export")).status_code, 200)

    def test_local_template_is_loaded_from_private_data_and_missing_file_is_supported(self):
        self.assertEqual(load_local_config()["categories"], example_categories())
        local_config_path().unlink()
        url = reverse("sap_integration:expense_categories")
        response = self.client.get(url)
        self.assertNotContains(response, "Lokale Kategorienvorlage prüfen")
        response = self.client.post(url, {"action": "defaults"})
        self.assertContains(response, "Keine lesbare lokale Kategorienvorlage")
        self.assertEqual(SAPExpenseCategory.objects.count(), 1)

    def test_old_personnel_position_is_classified_by_private_configuration(self):
        SAPExpenseCategory.objects.bulk_create([SAPExpenseCategory(**row) for row in example_personnel_categories()])
        self.write([booking(financial_position="99-0901", position="Personalbuchung ohne Gehaltstext")])
        self.assertEqual(self.checks(), [])

    def test_bulk_button_imports_new_mapped_bookings_directly_and_repeat_is_idempotent(self):
        self.write([booking(), booking(document_number="SECOND", type="commitment", amount="125.00", booking_date="2026-04-01")])
        url = reverse("sap_integration:expenses", args=[self.fund.pk]) + "?year=2026"
        page = self.client.get(url)
        self.assertContains(page, "Alles in Planung übernehmen (2)")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)
        data = {"action": "bulk_apply", "bulk_state_token": page.context["bulk_state_token"]}
        result = self.client.post(url, data, follow=True)
        self.assertContains(result, "2 Buchung(en) in die Planung übernommen")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 2)
        self.assertEqual(SAPExpenseLink.objects.count(), 2)
        self.assertEqual(SAPExpenseAudit.objects.count(), 2)
        self.assertEqual(set(SAPExpenseLink.objects.values_list("source_type", flat=True)), {"actual", "commitment"})
        self.assertEqual(sum(t.amount for t in OtherBudgetItemTransaction.objects.all()), Decimal("525.00"))
        repeated = self.client.post(url, data, follow=True)
        self.assertContains(repeated, "2 bereits verknüpfte Buchung(en) nicht erneut angelegt")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 2)
        self.assertEqual(SAPExpenseAudit.objects.count(), 2)

    def test_bulk_skips_ambiguous_unmapped_and_unknown_bookings_with_reasons(self):
        SAPExpenseCategory.objects.create(key="materials", label="Material", kind="other", exact_codes=["88000-0102"])
        self.write([booking(), booking(document_number="UNKNOWN", financial_position="99000-9999"),
                    booking(document_number="UNMAPPED", financial_position="88000-0102"),
                    booking(document_number="DUPLICATE"), booking(document_number="DUPLICATE", amount="50.00")])
        url = reverse("sap_integration:expenses", args=[self.fund.pk])
        page = self.client.get(url)
        self.assertEqual(page.context["bulk_count"], 1)
        result = self.client.post(url, {"action": "bulk_apply", "bulk_state_token": page.context["bulk_state_token"]}, follow=True)
        self.assertContains(result, "3 Buchung(en) ausgelassen")
        self.assertContains(result, "Kategorie fehlt")
        self.assertContains(result, "Budgetzuordnung fehlt")
        self.assertContains(result, "Beleg muss geprüft werden")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)
        self.assertEqual(SAPExpenseLink.objects.count(), 1)

    def test_bulk_does_not_update_existing_links_or_guess_manual_plan_associations(self):
        linked = self.apply()
        manual = self.planned()
        self.write([booking(amount="450.00"), booking(document_number="NEW", amount="125.00")])
        url = reverse("sap_integration:expenses", args=[self.fund.pk])
        page = self.client.get(url)
        self.assertEqual(page.context["bulk_count"], 1)
        self.client.post(url, {"action": "bulk_apply", "bulk_state_token": page.context["bulk_state_token"]})
        linked.planner_transaction.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(linked.planner_transaction.amount, Decimal("400.00"))
        self.assertEqual(manual.amount, Decimal("1000.00"))
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 3)

    def test_bulk_revalidates_source_and_budget_mapping_without_a_second_confirmation(self):
        url = reverse("sap_integration:expenses", args=[self.fund.pk])
        page = self.client.get(url)
        self.write([booking(amount="999.00")])
        result = self.client.post(url, {"action": "bulk_apply", "bulk_state_token": page.context["bulk_state_token"]}, follow=True)
        self.assertContains(result, "Daten seit dem Laden geändert")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)
        page = self.client.get(url)
        other_budget = OtherBudgetItem.objects.create(project=self.project, title="Anderes Budget", amount=2000)
        SAPExpenseBudgetMapping.objects.filter(fund=self.fund, category=self.category).update(budget_item=other_budget)
        result = self.client.post(url, {"action": "bulk_apply", "bulk_state_token": page.context["bulk_state_token"]}, follow=True)
        self.assertContains(result, "Daten seit dem Laden geändert")
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)

    def test_bulk_is_scoped_to_the_selected_fund_and_year(self):
        other_fund = SAPFund.objects.create(fund_number="OTHER-FUND", project=self.project)
        SAPExpenseBudgetMapping.objects.create(fund=other_fund, category=self.category, budget_item=self.budget)
        path = self.directory / "processed" / "2026.json"
        payload = json.loads(path.read_text())
        payload["funds"][other_fund.fund_number] = {"transactions": [booking(document_number="OTHER")]}
        path.write_text(json.dumps(payload))
        older = json.loads(json.dumps(payload))
        older["year"] = 2025
        older["funds"][self.fund.fund_number]["transactions"] = [booking(document_number="OLD", booking_date="2025-03-12")]
        path.with_name("2025.json").write_text(json.dumps(older))
        url = reverse("sap_integration:expenses", args=[self.fund.pk]) + "?year=2026"
        page = self.client.get(url)
        data = {"action": "bulk_apply", "bulk_state_token": page.context["bulk_state_token"]}
        self.client.post(reverse("sap_integration:expenses", args=[other_fund.pk]) + "?year=2026", data)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)
        self.client.post(url.replace("2026", "2025"), data)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)
        self.client.post(url, data)
        self.assertEqual(list(SAPExpenseLink.objects.values_list("fund_id", "year")), [(self.fund.pk, 2026)])

    def test_bulk_requires_staff_active_adapter_and_valid_state_and_get_never_writes(self):
        url = reverse("sap_integration:expenses", args=[self.fund.pk])
        page = self.client.get(url)
        data = {"action": "bulk_apply", "bulk_state_token": page.context["bulk_state_token"]}
        self.client.get(url, data)
        self.client.post(url, {"action": "bulk_apply", "bulk_state_token": "tampered"})
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)
        regular = get_user_model().objects.create_user("regular-bulk", password="x")
        self.client.force_login(regular)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.client.force_login(self.user)
        with override_settings(SAP_ENABLED=False, SAP_GM_IMPORT_ENABLED=True):
            self.assertEqual(self.client.post(url, data).status_code, 404)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)

    def test_bulk_unexpected_error_rolls_back_all_records_and_loads_cache_once(self):
        from sap_integration import expenses as service

        self.write([booking(), booking(document_number="SECOND")])
        checks = self.checks()
        expected = {c.key: bulk_expense_state(c) for c in checks if c.can_bulk_apply}
        original_save = service._save_expense
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertEqual(OtherBudgetItemTransaction.objects.count(), 1)
                raise RuntimeError("Unexpected failure")
            return original_save(*args, **kwargs)

        with patch.object(service, "build_expense_checks", wraps=service.build_expense_checks) as build:
            with patch.object(service, "_save_expense", side_effect=fail_second):
                with self.assertRaisesMessage(RuntimeError, "Unexpected failure"):
                    apply_expenses(self.directory, self.fund, 2026, expected_entries=expected, user=self.user)
            self.assertEqual(build.call_count, 1)
        self.assertEqual(OtherBudgetItemTransaction.objects.count(), 0)
        self.assertEqual(SAPExpenseLink.objects.count(), 0)
        self.assertEqual(SAPExpenseAudit.objects.count(), 0)
