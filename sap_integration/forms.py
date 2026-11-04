from django import forms

from projects.models import EmploymentCategories, OtherBudgetItem, OtherBudgetItemTransaction, StaffBudgetItem
from staffing.models import Employment, StaffFundingAllocation, StaffMember
from sap_integration.models import SAPExpenseCategory


class ExportUploadForm(forms.Form):
    export_file = forms.FileField(
        label="SAP-Export (GM_E_4GBA, .xlsx)",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx"}),
    )


class IgnoreForm(forms.Form):
    note = forms.CharField(label="Notiz", max_length=255, required=False)


class StaffTransformForm(forms.Form):
    staff_member = forms.ModelChoiceField(
        StaffMember.objects.order_by("last_name", "first_name"),
        label="Mitarbeiter",
        required=False,
        empty_label="– neu anlegen –",
    )
    first_name = forms.CharField(label="Vorname (neu)", max_length=100, required=False)
    last_name = forms.CharField(label="Nachname (neu)", max_length=100, required=False)
    remember_name = forms.BooleanField(
        label="SAP-Namen für künftige Importe diesem Mitarbeiter zuordnen",
        required=False,
        initial=True,
    )
    employment = forms.ModelChoiceField(
        Employment.objects.none(),
        label="Anstellung",
        required=False,
        empty_label="– neue Anstellung je SAP-Vertragszeitraum –",
        help_text="Direkt anschließende Vertragszeiträume verlängern die gewählte Anstellung.",
    )
    budget_item = forms.ModelChoiceField(StaffBudgetItem.objects.none(), label="Personalbudget")
    category = forms.ChoiceField(label="Kategorie", choices=list(EmploymentCategories.items()))
    percentage = forms.DecimalField(label="Umfang (%)", max_digits=5, decimal_places=2, min_value=0, max_value=100)
    create_salaries = forms.BooleanField(
        label="Gehälter aus SAP übernehmen (Ist je Monat, Restmonate mit letztem Monatsgehalt)",
        required=False,
        initial=True,
    )

    def __init__(self, *args, project, staff_member=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["budget_item"].queryset = StaffBudgetItem.objects.filter(project=project)
        employments = Employment.objects.select_related("staff_member").order_by("staff_member__last_name", "start_date")
        if staff_member is not None:
            employments = employments.filter(staff_member=staff_member)
        self.fields["employment"].queryset = employments

    def clean(self):
        cleaned = super().clean()
        staff_member = cleaned.get("staff_member")
        employment = cleaned.get("employment")
        if staff_member is None and not (cleaned.get("first_name") and cleaned.get("last_name")):
            raise forms.ValidationError("Bitte einen Mitarbeiter wählen oder Vor- und Nachname angeben.")
        if employment is not None and employment.staff_member_id != getattr(staff_member, "id", None):
            raise forms.ValidationError("Die gewählte Anstellung gehört nicht zum gewählten Mitarbeiter.")
        return cleaned


class LinkAllocationsForm(forms.Form):
    allocations = forms.ModelMultipleChoiceField(
        StaffFundingAllocation.objects.none(),
        label="Zuordnungen",
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allocations"].queryset = StaffFundingAllocation.objects.filter(
            budget_item__project=project
        ).select_related("employment__staff_member").order_by("employment__staff_member__last_name", "start_date")


class CreateTransactionForm(forms.Form):
    budget_item = forms.ModelChoiceField(OtherBudgetItem.objects.none(), label="Sachmittelbudget")

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["budget_item"].queryset = OtherBudgetItem.objects.filter(project=project)


class LinkTransactionForm(forms.Form):
    transaction = forms.ModelChoiceField(OtherBudgetItemTransaction.objects.none(), label="Planungseintrag")

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["transaction"].queryset = OtherBudgetItemTransaction.objects.filter(
            budget_item__project=project
        ).select_related("budget_item").order_by("date")


class CostTypeMappingForm(forms.Form):
    cost_type = forms.CharField(max_length=50, widget=forms.HiddenInput)
    target = forms.ChoiceField(label="Budget", required=False)

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target"].choices = budget_choices(project)


def budget_choices(project):
    choices = [("", "– keine Zuordnung –")]
    choices += [
        (f"staff:{item.id}", f"Personal: {item.title}")
        for item in StaffBudgetItem.objects.filter(project=project)
    ]
    choices += [
        (f"other:{item.id}", f"Sachmittel: {item.title}")
        for item in OtherBudgetItem.objects.filter(project=project)
    ]
    return choices


class CodeListField(forms.CharField):
    widget = forms.Textarea

    def prepare_value(self, value):
        return "\n".join(value) if isinstance(value, list) else value

    def to_python(self, value):
        import re

        return [part for part in re.split(r"[\s,;]+", value or "") if part]


class ExpenseCategoryForm(forms.ModelForm):
    exact_codes = CodeListField(label="Finanzpositionen", required=False,
                                help_text="Eine Nummer pro Zeile; auch Kommas sind möglich.")
    patterns = CodeListField(label="Wildcard-Muster", required=False,
                            help_text="Ein Muster pro Zeile. * steht für beliebig viele Zeichen, ? für genau eines. Beispiel: *-0101.")

    class Meta:
        model = SAPExpenseCategory
        fields = ["key", "label", "kind", "exact_codes", "patterns", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["key"].help_text = "Stabile Kennung für den Austausch mit anderen Installationen. Nach dem Anlegen unveränderlich."
        if self.instance.pk:
            self.fields["key"].disabled = True


class ExpenseConfigUploadForm(forms.Form):
    config_file = forms.FileField(label="Kategorienkonfiguration (.json)")

    def clean_config_file(self):
        from sap_integration.expense_config import MAX_CONFIG_BYTES

        uploaded = self.cleaned_data["config_file"]
        if uploaded.size > MAX_CONFIG_BYTES:
            raise forms.ValidationError("Die Konfiguration darf höchstens 256 KB groß sein.")
        return uploaded


class ExpenseTransactionChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.date:%d.%m.%Y} · {obj.amount} € · {obj.budget_item.title} · {(obj.description or '')[:120]}"


class ExpenseApplyForm(forms.Form):
    planner_transaction = ExpenseTransactionChoiceField(
        OtherBudgetItemTransaction.objects.none(), required=False, label="Bestehender Planungseintrag",
        empty_label="– neuen Eintrag anlegen –",
    )
    budget_item = forms.ModelChoiceField(OtherBudgetItem.objects.none(), label="Planungsbudget für den neuen Eintrag", required=False)

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["budget_item"].queryset = OtherBudgetItem.objects.filter(project=project)
        self.fields["planner_transaction"].queryset = OtherBudgetItemTransaction.objects.filter(
            budget_item__project=project
        ).select_related("budget_item").order_by("date", "id")

    def clean(self):
        data = super().clean()
        target = data.get("planner_transaction")
        if target:
            data["budget_item"] = target.budget_item
        elif not data.get("budget_item"):
            self.add_error("budget_item", "Bitte für den neuen Eintrag ein Planungsbudget auswählen.")
        return data
