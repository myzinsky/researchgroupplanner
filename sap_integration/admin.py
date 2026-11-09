import json

from django.contrib import admin, messages
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html

from sap_integration.models import (
    SAPCostTypeMapping,
    SAPIgnoredPosition,
    SAPImport,
    SAPPersonMapping,
    SAPExpenseCategory,
    SAPExpenseBudgetMapping,
    SAPExpenseLink,
    SAPExpenseAudit,
    SAPExpenseUnlinkArchive,
)
from sap_integration.expense_unlink import unlink_expenses, unlink_preview
from sap_integration.forms import ExpenseCategoryForm


@admin.register(SAPImport)
class SAPImportAdmin(admin.ModelAdmin):
    list_display = ("fund", "file_name", "last_booking", "imported_at", "imported_by")
    readonly_fields = ("fund", "file_name", "imported_at", "imported_by", "row_count", "first_booking", "last_booking", "cost_types")


@admin.register(SAPCostTypeMapping)
class SAPCostTypeMappingAdmin(admin.ModelAdmin):
    list_display = ("fund", "cost_type", "staff_budget_item", "other_budget_item")
    list_filter = ("fund",)


@admin.register(SAPPersonMapping)
class SAPPersonMappingAdmin(admin.ModelAdmin):
    list_display = ("sap_name", "staff_member")
    fields = ("sap_name", "staff_member")
    search_fields = ("sap_name", "staff_member__last_name", "staff_member__first_name")


@admin.register(SAPIgnoredPosition)
class SAPIgnoredPositionAdmin(admin.ModelAdmin):
    list_display = ("fund", "reference", "note", "created_by", "created_at")
    list_filter = ("fund",)
    search_fields = ("reference", "note")


@admin.register(SAPExpenseCategory)
class SAPExpenseCategoryAdmin(admin.ModelAdmin):
    form = ExpenseCategoryForm
    list_display = ("key", "label", "kind", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("key", "label")


@admin.register(SAPExpenseBudgetMapping)
class SAPExpenseBudgetMappingAdmin(admin.ModelAdmin):
    list_display = ("fund", "category", "budget_item")
    list_filter = ("fund",)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "category":
            kwargs["queryset"] = SAPExpenseCategory.objects.exclude(kind="staff")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class SAPExpenseAuditInline(admin.StackedInline):
    model = SAPExpenseAudit
    verbose_name = "Übernahmeprotokoll"
    verbose_name_plural = "Übernahmeprotokolle"
    extra = 0
    ordering = ("-created_at", "-pk")
    fields = ("created_at", "user", "action", "before", "after", "source_snapshot")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SAPExpenseLink)
class SAPExpenseLinkAdmin(admin.ModelAdmin):
    actions = ("unlink_selected",)
    list_display = ("id", "fund", "year", "source_type", "planning_budget", "booking_date", "amount", "is_active", "confirmed_at")
    list_filter = ("fund", "year", "source_type", "is_active")
    search_fields = (
        "=id", "fund__fund_number", "planner_transaction__budget_item__project__acronym",
        "planner_transaction__budget_item__title", "planner_transaction__description",
        "planner_transaction__sap_id", "source_key",
    )
    list_select_related = ("fund", "planner_transaction__budget_item__project")
    ordering = ("-confirmed_at", "-pk")
    fields = (
        "id", "fund", "year", "source_type", "source_key", "is_active",
        "planning_budget", "planner_transaction", "remainder_transaction",
        "confirmed_by", "confirmed_at", "source_snapshot", "planning_snapshot",
    )
    readonly_fields = fields
    inlines = (SAPExpenseAuditInline,)

    @admin.display(description="Planungsbudget", ordering="planner_transaction__budget_item__title")
    def planning_budget(self, obj):
        budget = obj.planner_transaction.budget_item
        url = reverse("admin:projects_otherbudgetitem_change", args=[budget.pk])
        return format_html('<a href="{}">{}</a>', url, budget)

    @admin.display(description="Buchungsdatum", ordering="planner_transaction__date")
    def booking_date(self, obj):
        return obj.planner_transaction.date

    @admin.display(description="Betrag", ordering="planner_transaction__amount")
    def amount(self, obj):
        return obj.planner_transaction.amount

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_unlink_permission(self, request):
        return request.user.has_perms((
            "sap_integration.delete_sapexpenselink", "sap_integration.delete_sapexpenseaudit",
            "sap_integration.view_sapexpenseaudit", "projects.change_otherbudgetitemtransaction",
        ))

    @admin.action(description="Ausgewählte Verknüpfungen lösen …", permissions=["unlink"])
    def unlink_selected(self, request, queryset):
        if not self.has_unlink_permission(request):
            raise PermissionDenied
        ids = sorted(queryset.values_list("pk", flat=True))
        salt = "sap-expense-unlink-v1"
        try:
            if request.POST.get("confirm_unlink") == "yes":
                state = signing.loads(request.POST.get("unlink_state", ""), salt=salt, max_age=3600)
                if state["user"] != request.user.pk or state["ids"] != ids:
                    raise ValidationError("Die Auswahl hat sich geändert. Bitte die Vorschau neu öffnen.")
                count = unlink_expenses(ids, expected_state=state["digest"], user=request.user)
                self.message_user(request, f"{count} Verknüpfung(en) gelöst und archiviert. Die Planungseinträge bleiben bestehen.", messages.SUCCESS)
                return None
            links, _, fingerprint = unlink_preview(ids)
        except (ValidationError, signing.BadSignature) as error:
            text = " ".join(error.messages) if isinstance(error, ValidationError) else "Die Bestätigung ist ungültig oder abgelaufen. Bitte die Vorschau neu öffnen."
            self.message_user(request, text, messages.ERROR)
            return None
        request.current_app = self.admin_site.name
        return TemplateResponse(request, "admin/sap_integration/unlink_expenses.html", {
            **self.admin_site.each_context(request),
            "title": "SAP-Ausgabenverknüpfungen lösen", "opts": self.opts,
            "links": links,
            "unlink_state": signing.dumps({"user": request.user.pk, "ids": ids, "digest": fingerprint}, salt=salt, compress=True),
        })


@admin.register(SAPExpenseUnlinkArchive)
class SAPExpenseUnlinkArchiveAdmin(admin.ModelAdmin):
    list_display = ("original_link_id", "fund_number", "year", "budget_label", "created_by", "created_at")
    list_filter = ("fund_number", "year")
    search_fields = ("=original_link_id", "fund_number", "budget_label")
    fields = ("original_link_id", "fund_number", "year", "budget_label", "created_by", "created_at", "archived_data")
    readonly_fields = fields

    @admin.display(description="Archivdaten")
    def archived_data(self, obj):
        return format_html("<pre>{}</pre>", json.dumps(obj.snapshot, ensure_ascii=False, indent=2))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
