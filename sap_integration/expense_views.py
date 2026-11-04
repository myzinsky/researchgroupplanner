"""Staff-only Würzburg configuration and confirmed expense reconciliation."""

import json
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

from controlling.utils import render
from projects.models import OtherBudgetItem, SAPFund
from sap_integration.cache import available_years, load_year
from sap_integration.category_rules import classify, digest
from sap_integration.expense_config import apply_config, export_config, preview_config, read_config, load_local_config, local_config_path
from sap_integration.expenses import apply_expense, build_expense_checks, preview_expense, transaction_state
from sap_integration.forms import ExpenseApplyForm, ExpenseCategoryForm, ExpenseConfigUploadForm
from sap_integration.models import SAPExpenseBudgetMapping, SAPExpenseCategory
from sap_integration.config import financial_positions_enabled


CONFIG_SALT = "wuerzburg-category-preview-v1"
EXPENSE_STATE_SALT = "wuerzburg-expense-state-v1"


def ensure_enabled():
    if not financial_positions_enabled():
        raise Http404("Der SAP-Abgleich nach Finanzposition ist für den aktiven Zugang nicht verfügbar.")


def error_message(error):
    return " ".join(error.messages) if isinstance(error, ValidationError) else str(error)


def observed_codes():
    codes = set()
    for year in available_years(settings.SAP_DATA_DIR):
        try:
            payload = load_year(settings.SAP_DATA_DIR, year)
        except (ValueError, OSError):
            continue
        for values in payload.get("funds", {}).values():
            codes.update(r["financial_position"] for r in values.get("transactions", []) if r.get("financial_position"))
    return sorted(codes)


def rule_matches(categories):
    result = []
    for code in observed_codes():
        category, matches = classify(code, categories)
        result.append({"code": code, "category": category,
                       "conflict": len(matches) > 1, "matches": ", ".join(c.label for c in matches),
                       "is_income": not matches and code.startswith("28")})
    return result


@staff_member_required
def categories(request):
    ensure_enabled()
    entry = get_object_or_404(SAPExpenseCategory, key=request.GET["edit"]) if request.GET.get("edit") else None
    action = request.POST.get("action")
    form = ExpenseCategoryForm(request.POST if action in {"save", "check"} else None, instance=entry)
    upload_form = ExpenseConfigUploadForm(request.POST or None, request.FILES or None)
    preview = None
    token = None
    candidate_categories = list(SAPExpenseCategory.objects.all())
    if request.method == "POST":
        try:
            if action in {"save", "check"} and form.is_valid():
                if action == "save":
                    form.save()
                    messages.success(request, "Ausgabenkategorie gespeichert. Bestehende Planungswerte wurden nicht geändert.")
                    return redirect("sap_integration:expense_categories")
                proposed = form.instance
                candidate_categories = [c for c in candidate_categories if c.pk != proposed.pk] + [proposed]
            elif action in {"upload", "defaults"}:
                if action == "defaults":
                    payload = load_local_config()
                elif upload_form.is_valid():
                    payload = read_config(upload_form.cleaned_data["config_file"].read())
                else:
                    payload = None
                if payload is not None:
                    preview = preview_config(payload)
                    token = signing.dumps({"payload": payload, "digest": digest(export_config())}, salt=CONFIG_SALT, compress=True)
                    incoming_keys = {row["key"] for row in payload["categories"]}
                    candidate_categories = [c for c in candidate_categories if c.key not in incoming_keys]
                    candidate_categories += [SAPExpenseCategory(**row) for row in payload["categories"]]
            elif action == "import":
                signed = signing.loads(request.POST.get("preview_token", ""), salt=CONFIG_SALT, max_age=3600)
                count = apply_config(signed["payload"], expected_digest=signed["digest"], replace_keys=set(request.POST.getlist("replace")))
                messages.success(request, f"{count} Kategorie(n) importiert. Projektbudgets und Buchungen wurden nicht geändert.")
                return redirect("sap_integration:expense_categories")
        except (ValidationError, signing.BadSignature) as error:
            messages.error(request, error_message(error))
    return render(request, "sap_integration/expense_categories.html", {
        "categories": SAPExpenseCategory.objects.all(), "category_form": form, "editing": entry,
        "upload_form": upload_form, "preview": preview, "preview_token": token,
        "local_templates_available": local_config_path().is_file(),
        "rule_matches": rule_matches(candidate_categories),
    })


@staff_member_required
def export_categories(request):
    ensure_enabled()
    response = HttpResponse(json.dumps(export_config(), ensure_ascii=False, indent=2) + "\n", content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="sap-finanzpositionskategorien.json"'
    return response


def load_fund(fund_id):
    return get_object_or_404(SAPFund.objects.select_related("project"), pk=fund_id, is_active=True)


def selected_year(request):
    years = available_years(settings.SAP_DATA_DIR)
    try:
        year = int(request.GET.get("year") or (years[0] if years else timezone.localdate().year))
    except ValueError as error:
        raise Http404("Ungültiges Geschäftsjahr.") from error
    if not 2000 <= year <= 2100:
        raise Http404("Ungültiges Geschäftsjahr.")
    return year, years


@staff_member_required
def expenses(request, fund_id):
    ensure_enabled()
    fund = load_fund(fund_id)
    year, years = selected_year(request)
    if request.method == "POST" and request.POST.get("action") == "map":
        category = get_object_or_404(SAPExpenseCategory, pk=request.POST.get("category"), kind__in=["travel", "other"])
        budget_id = request.POST.get("budget_item")
        if budget_id:
            budget = get_object_or_404(OtherBudgetItem, pk=budget_id, project_id=fund.project_id)
            mapping = SAPExpenseBudgetMapping(fund=fund, category=category, budget_item=budget)
            mapping.clean()
            SAPExpenseBudgetMapping.objects.update_or_create(fund=fund, category=category, defaults={"budget_item": budget})
        else:
            SAPExpenseBudgetMapping.objects.filter(fund=fund, category=category).delete()
        messages.success(request, "Budgetzuordnung gespeichert. Planungswerte wurden nicht geändert.")
        return redirect(f"{reverse('sap_integration:expenses', args=[fund.pk])}?year={year}")
    checks, cache_error = [], None
    try:
        checks = build_expense_checks(settings.SAP_DATA_DIR, fund, year)
    except (ValueError, ValidationError, OSError) as error:
        cache_error = error_message(error)
    summaries = defaultdict(lambda: {"actual": Decimal("0"), "commitment": Decimal("0")})
    for check in checks:
        # These are source totals, not planning totals: even a commitment whose
        # planning was replaced by an actual must remain visible as open in SAP.
        if not check.missing:
            summaries[check.category.pk if check.category else None][check.source_type] += check.amount
    mapping_by_category = {m.category_id: m.budget_item_id for m in SAPExpenseBudgetMapping.objects.filter(fund=fund)}
    category_rows = [{"category": category, "budget_id": mapping_by_category.get(category.pk),
                      **summaries[category.pk]} for category in SAPExpenseCategory.objects.exclude(kind="staff")]
    show_all = request.GET.get("all") == "1"
    return render(request, "sap_integration/expenses.html", {
        "fund": fund, "year": year, "years": years, "checks": [c for c in checks if show_all or c.is_open],
        "show_all": show_all, "open_count": sum(c.is_open for c in checks), "cache_error": cache_error,
        "category_rows": category_rows, "unknown_totals": summaries[None],
        "budget_items": OtherBudgetItem.objects.filter(project_id=fund.project_id) if fund.project_id else [],
    })


@staff_member_required
def expense_detail(request, fund_id, year, source_key):
    ensure_enabled()
    fund = load_fund(fund_id)
    try:
        check = next((c for c in build_expense_checks(settings.SAP_DATA_DIR, fund, year) if c.key == source_key), None)
    except (ValueError, ValidationError, OSError) as error:
        raise Http404(error_message(error)) from error
    if check is None:
        raise Http404("Die SAP-Buchung wurde nicht gefunden.")
    link = check.link
    initial = {"budget_item": link.planner_transaction.budget_item if link else check.budget_item,
               "planner_transaction": link.planner_transaction if link else None}
    form = ExpenseApplyForm(request.POST if request.method == "POST" else None, project=fund.project, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            if request.POST.get("action") != "apply":
                raise ValidationError("Bitte die aktualisierte Übernahmeansicht neu laden.")
            signed = signing.loads(request.POST.get("state_token", ""), salt=EXPENSE_STATE_SALT, max_age=3600)
            if signed["user"] != request.user.pk or signed["fund"] != fund.pk or signed["year"] != year or signed["key"] != source_key:
                raise ValidationError("Diese Ansicht gehört zu einem anderen Abgleich. Bitte die Seite neu laden.")
            target = form.cleaned_data.get("planner_transaction")
            if (signed["source"] != digest(check.snapshot) or (target and
                    signed["targets"].get(str(target.pk)) != digest(transaction_state(target)))):
                raise ValidationError("SAP-Daten oder Planung wurden inzwischen geändert. Bitte die aktuellen Werte prüfen und erneut übernehmen.")
            # The state check is transparent to the user. The atomic service
            # re-reads it once more before writing; there is no second UI step.
            preview = preview_expense(check, form.cleaned_data)
            apply_expense(settings.SAP_DATA_DIR, fund, year, source_key, form.cleaned_data,
                          expected_preview=preview, user=request.user)
            messages.success(request, "SAP-Werte in die Planung übernommen.")
            return redirect(f"{reverse('sap_integration:expenses', args=[fund.pk])}?year={year}")
        except (ValidationError, signing.BadSignature) as error:
            form.add_error(None, error_message(error))
        except IntegrityError:
            form.add_error(None, "Die Buchung oder der Planungseintrag wurde bereits verknüpft. Bitte den Abgleich neu öffnen.")
    candidates = []
    entries = list(form.fields["planner_transaction"].queryset)
    token = signing.dumps({"user": request.user.pk, "fund": fund.pk, "year": year,
                           "key": source_key, "source": digest(check.snapshot),
                           "targets": {str(entry.pk): digest(transaction_state(entry)) for entry in entries}},
                          salt=EXPENSE_STATE_SALT, compress=True)
    if not link and fund.project_id:
        from difflib import SequenceMatcher

        for entry in entries:
            similarity = SequenceMatcher(None, check.description.lower(), (entry.description or "").lower()).ratio()
            if entry.amount == check.amount or similarity > 0.35:
                candidates.append({"entry": entry, "score": similarity + (1 if entry.amount == check.amount else 0)})
        candidates = sorted(candidates, key=lambda candidate: candidate["score"], reverse=True)[:5]
    return render(request, "sap_integration/expense_detail.html", {
        "fund": fund, "year": year, "check": check, "apply_form": form,
        "state_token": token, "candidates": candidates,
        "audit_entries": link.audit_entries.select_related("user").order_by("-created_at")[:10] if link else [],
    })
