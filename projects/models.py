from django.conf import settings
from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.exceptions import ValidationError
from decimal import Decimal

EmploymentCategories = {
    'professor': 'Professoren',
    'student': 'Studentische Hilfskraft',
    'undergrad': 'Nicht-Wissenschaftliche Mitarbeiter',
    'researcher': 'Wissenschaftliche Mitarbeiter',
}

# Create your models here.
class Project(models.Model):
    acronym = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    extension_planning_date = models.DateField(blank=True, null=True)
    budget_total = models.DecimalField(max_digits=10, decimal_places=2)
    no_overhead = models.BooleanField(default=False, verbose_name="Kein Overhead")

    def clean(self):
        super().clean()
        if self.extension_planning_date and self.extension_planning_date < self.end_date:
            raise ValidationError({
                "extension_planning_date": "Die kostenneutrale Verlängerung darf nicht vor dem regulären Enddatum liegen."
            })

    def __str__(self):
        return self.acronym

    def get_years(self):
        return [str(i) for i in range(self.start_date.year, self.end_date.year + 1)]

    def get_effective_end_date(self):
        if self.extension_planning_date and self.extension_planning_date > self.end_date:
            return self.extension_planning_date
        return self.end_date


class ProjectMilestone(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    date = models.DateField()
    title = models.CharField(max_length=200)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f"{self.project.acronym} - {self.title} ({self.date})"


class ProjectContact(models.Model):
    """Person involved in a project (e.g. project officer, coordinator)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField("Name", max_length=200)
    email = models.EmailField("E-Mail", blank=True)
    function = models.CharField("Funktion", max_length=200)

    class Meta:
        ordering = ["function", "name"]
        verbose_name = "Ansprechperson"
        verbose_name_plural = "Ansprechpersonen"

    def __str__(self):
        return f"{self.project.acronym} – {self.name} ({self.function})"


class ProjectLink(models.Model):
    """Additional link of a project, e.g. to the project website or a shared folder."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="links")
    title = models.CharField("Titel", max_length=200)
    url = models.URLField("URL", max_length=500)

    class Meta:
        ordering = ["title"]
        verbose_name = "Link"
        verbose_name_plural = "Links"

    def __str__(self):
        return f"{self.project.acronym} – {self.title}"


class ProjectDeliverable(models.Model):
    """Deliverable of a project with its deadline."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="deliverables")
    title = models.CharField("Titel", max_length=200)
    deadline = models.DateField("Deadline")
    is_lead = models.BooleanField("Federführung", default=False, help_text="Wir sind für das Deliverable verantwortlich.")

    class Meta:
        ordering = ["deadline", "title"]
        verbose_name = "Deliverable"
        verbose_name_plural = "Deliverables"

    def __str__(self):
        return f"{self.project.acronym} – {self.title} ({self.deadline:%d.%m.%Y})"


class ReportingPeriod(models.Model):
    """Additional reporting period (Berichtszeitraum) of a project."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="reporting_periods")
    title = models.CharField("Bezeichnung", max_length=200, blank=True)
    start_date = models.DateField("Beginn")
    end_date = models.DateField("Ende")

    class Meta:
        ordering = ["start_date"]
        verbose_name = "Berichtszeitraum"
        verbose_name_plural = "Berichtszeiträume"

    def __str__(self):
        return f"{self.project.acronym} – {self.label}"

    @property
    def label(self):
        dates = f"{self.start_date:%d.%m.%Y} – {self.end_date:%d.%m.%Y}"
        return f"{self.title} ({dates})" if self.title else dates

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("Das Ende darf nicht vor dem Beginn liegen.")


class Institute(models.Model):
    name = models.CharField(max_length=200, unique=True)
    short_name = models.CharField(max_length=50, unique=True)
    is_own_chair = models.BooleanField(
        default=False,
        help_text="Markiert den eigenen Lehrstuhl. Overhead-Anteile dieses Instituts gelten als verfügbar.",
    )

    def __str__(self):
        return self.short_name


class Landesstelle(models.Model):
    title = models.CharField(max_length=200)
    institute = models.ForeignKey(Institute, on_delete=models.PROTECT, null=True, blank=True)
    percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    start_date = models.DateField()
    end_date = models.DateField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    def __str__(self):
        institute_str = f" ({self.institute.short_name})" if self.institute_id else ""
        if self.end_date:
            return f"{self.title}{institute_str} {self.percentage}% ({self.start_date} - {self.end_date})"
        return f"{self.title}{institute_str} {self.percentage}% (ab {self.start_date})"

class StaffBudgetItem(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    title = models.CharField(max_length=100)
    short_title = models.CharField(max_length=50, blank=True, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)


    def __str__(self):
        return f"{self.project.acronym} - {self.title}"

    def get_eligibilities(self):
        return StaffBudgetItemEligibility.objects.filter(budget_item=self)

class StaffBudgetItemEligibility(models.Model):
    budget_item = models.ForeignKey(StaffBudgetItem, on_delete=models.CASCADE)
    eligible_employment = models.CharField(max_length=50, choices=EmploymentCategories)

class OverheadBudgetItem(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    def available_amount(self):
        """Sum of shares belonging to institutes marked as own chair."""
        if not settings.OVERHEAD_SPLIT_ENABLED:
            return self.amount
        total = Decimal("0.00")
        for share in self.overheadbudgetitemshare_set.filter(institute__is_own_chair=True):
            total += (self.amount * share.percentage / Decimal("100")).quantize(Decimal("0.01"))
        return total

    def own_chair_percentage(self):
        if not settings.OVERHEAD_SPLIT_ENABLED:
            return Decimal("100.00")
        return sum(
            share.percentage
            for share in self.overheadbudgetitemshare_set.filter(institute__is_own_chair=True)
        )

    def __str__(self):
        return f"{self.project.acronym} - Overhead - {self.amount}"


class OverheadBudgetItemShare(models.Model):
    overhead_item = models.ForeignKey(OverheadBudgetItem, on_delete=models.CASCADE)
    institute = models.ForeignKey(Institute, on_delete=models.PROTECT)
    percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    class Meta:
        unique_together = [("overhead_item", "institute")]

    def __str__(self):
        return f"{self.overhead_item} → {self.institute}: {self.percentage}%"

class OtherBudgetItem(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    title = models.CharField(max_length=100)
    short_title = models.CharField(max_length=50, blank=True, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.project.acronym} - {self.title} - {self.amount}"
    
    def get_transactions(self, year: int = None) -> list['OtherBudgetItemTransaction']:
        if year:
            return list(OtherBudgetItemTransaction.objects.filter(budget_item=self, date__year=year))
        return list(OtherBudgetItemTransaction.objects.filter(budget_item=self))

class OtherBudgetItemTransaction(models.Model):
    budget_item = models.ForeignKey(OtherBudgetItem, on_delete=models.CASCADE)
    date = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.TextField(blank=True, null=True)
    sap_id = models.CharField(max_length=100, blank=True, null=True)

    def __str__(self):
        return f"{self.budget_item} - {self.date} - {self.amount}"


class AnnualPool(models.Model):
    title = models.CharField(max_length=200, unique=True)
    notes = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.title


class SAPFund(models.Model):
    fund_number = models.CharField("Fondsnummer", max_length=100, unique=True)
    label = models.CharField("Bezeichnung", max_length=200, blank=True)
    is_active = models.BooleanField("Aktiv", default=True)
    is_universal = models.BooleanField("Universalprojekt", default=False)
    treat_negative_actuals_as_funding = models.BooleanField(
        "Negative Ist-Buchungen als Mittelzuflüsse behandeln",
        default=False,
        help_text=(
            "Nach dem Entfernen von Gegenbuchungen werden verbleibende negative "
            "Ist-Buchungen angezeigt, aber nicht als Ausgaben verrechnet."
        ),
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="sap_funds",
        null=True,
        blank=True,
    )
    annual_pool = models.ForeignKey(
        AnnualPool,
        on_delete=models.CASCADE,
        related_name="sap_funds",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["fund_number"]
        verbose_name = "SAP-Fonds"
        verbose_name_plural = "SAP-Fonds"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        is_universal=False,
                        project__isnull=False,
                        annual_pool__isnull=True,
                    )
                    | models.Q(
                        is_universal=False,
                        project__isnull=True,
                        annual_pool__isnull=False,
                    )
                    | models.Q(
                        is_universal=True,
                        project__isnull=True,
                        annual_pool__isnull=True,
                    )
                ),
                name="sap_fund_has_valid_owner",
                violation_error_message=(
                    "Ein SAP-Fonds muss einem Projekt oder Annual Pool zugeordnet "
                    "oder als Universalprojekt markiert sein."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.fund_number} – {self.label}" if self.label else self.fund_number


class AnnualPoolBudget(models.Model):
    annual_pool = models.ForeignKey(AnnualPool, on_delete=models.CASCADE)
    year = models.PositiveIntegerField()
    amount_assigned = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = [("annual_pool", "year")]
        ordering = ["year"]

    def __str__(self):
        return f"{self.annual_pool.title} - {self.year}: {self.amount_assigned}"
