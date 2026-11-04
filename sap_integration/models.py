from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from projects.models import OtherBudgetItem, OtherBudgetItemTransaction, SAPFund, StaffBudgetItem
from staffing.models import StaffMember
from sap_integration.category_rules import validate_exact_codes, validate_patterns


# FMM-Werttyp of manual transfer postings between funds.
TRANSFER_VALUE_TYPE = "66"
# FMM-Werttyp of Mittelreservierungen (not reduced by the actual payment).
RESERVATION_VALUE_TYPE = "81"
# FMM-Werttypen of actual bookings (payments, transfers).
ACTUAL_VALUE_TYPES = {"66", "99", "Z1"}


class SAPPositionKind(models.TextChoices):
    STAFF = "staff", "Personal"
    TRAVEL = "travel", "Reise"
    OTHER = "other", "Sachmittel"
    INCOME = "income", "Einnahme"


class SAPImport(models.Model):
    """Latest imported project export (GM_E_4GBA) for one SAP fund."""

    fund = models.ForeignKey(SAPFund, on_delete=models.CASCADE, related_name="sap_imports")
    file_name = models.CharField("Datei", max_length=255)
    imported_at = models.DateTimeField("Importiert am", auto_now_add=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    row_count = models.PositiveIntegerField("Buchungszeilen", default=0)
    first_booking = models.DateField(null=True, blank=True)
    last_booking = models.DateField(null=True, blank=True)
    # {cost_type: {"budget": "…", "actual": "…", "commitment": "…"}} as decimal strings.
    cost_types = models.JSONField(default=dict)

    class Meta:
        ordering = ["-imported_at"]
        verbose_name = "Projektexport (GM)"
        verbose_name_plural = "Projektexporte (GM)"

    def __str__(self):
        return f"{self.fund.fund_number} ({self.imported_at:%d.%m.%Y %H:%M})"

    def cost_type_values(self):
        return {
            cost_type: {key: Decimal(value) for key, value in values.items()}
            for cost_type, values in self.cost_types.items()
        }


class SAPPosition(models.Model):
    """One SAP document chain (reservation, order, travel, …) with its bookings."""

    sap_import = models.ForeignKey(SAPImport, on_delete=models.CASCADE, related_name="positions")
    reference = models.CharField("Referenzbelegnummer", max_length=50)
    kind = models.CharField(max_length=20, choices=SAPPositionKind.choices)
    cost_type = models.CharField("E/A-Art", max_length=50)
    title = models.CharField("Bezeichnung", max_length=255, blank=True)
    description = models.TextField("Text", blank=True)
    person_name = models.CharField("Person", max_length=200, blank=True)
    contract_type = models.CharField("Vertragsart", max_length=20, blank=True)
    # [["2023-10-01", "2024-12-31"], …] parsed from the reservation texts.
    contract_periods = models.JSONField(default=list)
    percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    weekly_hours = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    first_date = models.DateField(null=True, blank=True)
    last_date = models.DateField(null=True, blank=True)
    actual = models.DecimalField("Ist", max_digits=12, decimal_places=2, default=Decimal("0.00"))
    commitment = models.DecimalField("Obligo", max_digits=12, decimal_places=2, default=Decimal("0.00"))
    # {"2024-01": "5445.01"} payroll per salary month for staff positions.
    monthly_actuals = models.JSONField(default=dict)
    # [{"date", "value_type", "amount", "text"}] for display.
    bookings = models.JSONField(default=list)

    class Meta:
        ordering = ["kind", "person_name", "first_date", "reference"]
        unique_together = [("sap_import", "reference")]
        verbose_name = "SAP-Position"
        verbose_name_plural = "SAP-Positionen"

    def __str__(self):
        return f"{self.reference} {self.title}".strip()

    @property
    def total(self):
        return self.actual + self.commitment

    @property
    def actual_date(self):
        """Date of the last actual booking (e.g. the invoice payment), or None if nothing was paid."""
        dates = [
            b["date"] for b in self.bookings
            if b.get("value_type") in ACTUAL_VALUE_TYPES and b.get("date") and Decimal(b["amount"])
        ]
        return date.fromisoformat(max(dates)) if dates else None

    @property
    def reservation_commitment(self):
        """Commitment from Mittelreservierungen (FMM-Werttyp 81), e.g. travel reservations."""
        return sum(
            (Decimal(b["amount"]) for b in self.bookings if b.get("value_type") == RESERVATION_VALUE_TYPE),
            Decimal("0.00"),
        )

    @property
    def planned_amount(self):
        """Amount the planning should carry.

        Orders (requisitions/purchase orders) are reduced by SAP with each
        invoice, so their open commitment is still to come. A Mittelreservierung
        is not reduced by the payment: once anything was paid only the actual
        counts and a remaining reservation is reported (uncleared_commitment).
        """
        reservation = self.reservation_commitment
        order_commitment = self.commitment - reservation
        return self.actual + order_commitment + (Decimal("0.00") if self.actual else reservation)

    @property
    def uncleared_commitment(self):
        """Reservation still open although actual bookings exist (not cleared in SAP)."""
        reservation = self.reservation_commitment
        return reservation if self.actual and reservation else Decimal("0.00")

    @property
    def is_transfer(self):
        """Staff cost transfer between funds (Umbuchung), not a contract of its own."""
        return (
            self.kind == SAPPositionKind.STAFF
            and not self.person_name
            and not self.contract_periods
            and bool(self.bookings)
            and all(booking["value_type"] == TRANSFER_VALUE_TYPE for booking in self.bookings)
        )


class SAPCostTypeMapping(models.Model):
    """Maps an SAP E/A-Art of a fund onto a planner budget item."""

    fund = models.ForeignKey(SAPFund, on_delete=models.CASCADE, related_name="cost_type_mappings")
    cost_type = models.CharField("E/A-Art", max_length=50)
    staff_budget_item = models.ForeignKey(
        StaffBudgetItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name="Personalbudget",
    )
    other_budget_item = models.ForeignKey(
        OtherBudgetItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name="Sachmittelbudget",
    )

    class Meta:
        ordering = ["fund", "cost_type"]
        unique_together = [("fund", "cost_type")]
        verbose_name = "E/A-Art-Zuordnung (GM)"
        verbose_name_plural = "E/A-Art-Zuordnungen (GM)"

    def clean(self):
        super().clean()
        if self.staff_budget_item_id and self.other_budget_item_id:
            raise ValidationError("Bitte höchstens ein Budget angeben.")
        project_id = self.fund.project_id if self.fund_id else None
        for item in (self.staff_budget_item, self.other_budget_item):
            if item is not None and item.project_id != project_id:
                raise ValidationError("Das Budget muss zum Projekt des SAP-Fonds gehören.")

    def budget_item(self):
        return self.staff_budget_item or self.other_budget_item

    def __str__(self):
        return f"{self.fund.fund_number} {self.cost_type} → {self.budget_item() or '–'}"


class SAPPersonMapping(models.Model):
    """Remembers which staff member an SAP person name refers to."""

    name_key = models.CharField(max_length=200, unique=True)
    sap_name = models.CharField("Name in SAP", max_length=200)
    staff_member = models.ForeignKey(
        StaffMember,
        on_delete=models.CASCADE,
        related_name="sap_name_mappings",
        verbose_name="Mitarbeiter",
    )

    class Meta:
        ordering = ["sap_name"]
        verbose_name = "Namenszuordnung (GM)"
        verbose_name_plural = "Namenszuordnungen (GM)"

    def save(self, *args, **kwargs):
        from sap_integration.names import name_key

        self.name_key = name_key(self.sap_name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sap_name} → {self.staff_member}"


class SAPIgnoredPosition(models.Model):
    """SAP positions stashed as irrelevant; kept across re-imports."""

    fund = models.ForeignKey(SAPFund, on_delete=models.CASCADE, related_name="ignored_positions")
    reference = models.CharField("Referenzbelegnummer", max_length=50)
    note = models.CharField("Notiz", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        unique_together = [("fund", "reference")]
        verbose_name = "Ignorierte Position (GM)"
        verbose_name_plural = "Ignorierte Positionen (GM)"

    def __str__(self):
        return f"{self.fund.fund_number} {self.reference}"


class SAPExpenseCategory(models.Model):
    """Institution-wide Würzburg rules; keys travel between installations."""

    key = models.SlugField("Kennung", max_length=80, unique=True)
    label = models.CharField("Bezeichnung", max_length=200)
    kind = models.CharField("Kostenart", max_length=20, choices=[("travel", "Reisen"), ("other", "Sachmittel"), ("staff", "Personal")])
    exact_codes = models.JSONField("Finanzpositionen", default=list, blank=True, validators=[validate_exact_codes])
    patterns = models.JSONField("Wildcard-Muster", default=list, blank=True, validators=[validate_patterns])
    is_active = models.BooleanField("Aktiv", default=True)

    class Meta:
        ordering = ["label"]
        verbose_name = "Ausgabenkategorie (Finanzposition)"
        verbose_name_plural = "Ausgabenkategorien (Finanzposition)"

    def __str__(self):
        return self.label

    def clean(self):
        super().clean()
        # JSONField skips validators for some empty values (e.g. {}), but the
        # matching engine always needs lists, including for an empty rule set.
        validate_exact_codes(self.exact_codes)
        validate_patterns(self.patterns)
        if not self.exact_codes and not self.patterns:
            raise ValidationError("Bitte mindestens eine Finanzposition oder ein Muster angeben.")


class SAPExpenseBudgetMapping(models.Model):
    fund = models.ForeignKey(SAPFund, on_delete=models.CASCADE)
    category = models.ForeignKey(SAPExpenseCategory, on_delete=models.CASCADE)
    budget_item = models.ForeignKey(OtherBudgetItem, on_delete=models.CASCADE, verbose_name="Planungsbudget")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["fund", "category"], name="unique_wuerzburg_category_budget")]
        verbose_name = "Budgetzuordnung (Finanzposition)"
        verbose_name_plural = "Budgetzuordnungen (Finanzposition)"

    def clean(self):
        super().clean()
        if self.category_id and self.category.kind == "staff":
            raise ValidationError("Personalkategorien werden über den Gehaltsabgleich behandelt und keinem Sachmittelbudget zugeordnet.")
        if self.fund_id and self.budget_item_id and (
            not self.fund.project_id or self.fund.project_id != self.budget_item.project_id
        ):
            raise ValidationError("Das Budget muss zum Projekt des SAP-Fonds gehören.")

    def __str__(self):
        return f"{self.fund} / {self.category} → {self.budget_item}"


class SAPExpenseLink(models.Model):
    """Stable document component identity, never tied to a disposable cache row.

    Ambiguous components are blocked before creating a link. Superseded
    commitments stay recorded so a later download cannot import them again.
    """

    fund = models.ForeignKey(SAPFund, on_delete=models.PROTECT)
    year = models.PositiveIntegerField()
    source_key = models.CharField(max_length=64)
    source_type = models.CharField(max_length=20, choices=[("actual", "Ist"), ("commitment", "Obligo")])
    planner_transaction = models.ForeignKey(OtherBudgetItemTransaction, on_delete=models.PROTECT, related_name="wuerzburg_links")
    remainder_transaction = models.ForeignKey(
        OtherBudgetItemTransaction, null=True, blank=True, on_delete=models.PROTECT,
        related_name="wuerzburg_remainders",
    )
    is_active = models.BooleanField(default=True)
    # A copy of the exact last confirmed source and planning state for review.
    source_snapshot = models.JSONField(default=dict)
    planning_snapshot = models.JSONField(default=dict)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    confirmed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["fund", "year", "source_key"], name="unique_wuerzburg_expense_source"),
            models.UniqueConstraint(fields=["planner_transaction"], condition=models.Q(is_active=True), name="unique_active_wuerzburg_target"),
        ]


class SAPExpenseAudit(models.Model):
    link = models.ForeignKey(SAPExpenseLink, on_delete=models.PROTECT, related_name="audit_entries")
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=30)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    source_snapshot = models.JSONField(default=dict)
