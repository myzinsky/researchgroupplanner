from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from controlling.utils import render
from projects.models import SAPFund
from sap_integration.cache import SAPCacheError, available_years, fund_values, load_year
from sap_integration.cleaning import clean_fund_values


def _ensure_enabled():
    if not settings.SAP_ENABLED:
        raise Http404("Die SAP-Integration ist deaktiviert.")


def _owner(fund):
    if fund.project_id:
        return fund.project.acronym, "Projekt"
    if fund.annual_pool_id:
        return fund.annual_pool.title, "Annual Pool"
    return "Universalprojekt", "Universalprojekt"


def _generated_at(payload):
    value = payload.get("generated_at") if payload else None
    return parse_datetime(value) if value else None


def _has_year_data(values):
    return values is not None and (
        values.get("has_budget") or bool(values.get("transactions"))
    )


def _display_values(fund, cached_fund):
    values = fund_values(cached_fund)
    if values is not None and fund.treat_negative_actuals_as_funding:
        return clean_fund_values(
            values,
            treat_negative_actuals_as_funding=True,
        )
    return values


def _project_time_percentage(project, today):
    start_date = project.start_date
    end_date = project.get_effective_end_date()
    total_days = (end_date - start_date).days
    if total_days <= 0:
        return Decimal("100.00") if today >= end_date else Decimal("0.00")

    elapsed_days = (today - start_date).days
    bounded_days = min(max(elapsed_days, 0), total_days)
    return (
        Decimal(bounded_days) / Decimal(total_days) * Decimal("100")
    ).quantize(Decimal("0.01"))


def _project_lifetime_summaries(funds, payloads, today=None):
    today = today or timezone.localdate()
    used_by_project = defaultdict(lambda: Decimal("0"))
    projects = {}

    for fund in funds:
        if not fund.project_id:
            continue
        projects[fund.project_id] = fund.project
        for payload in payloads.values():
            values = _display_values(
                fund,
                payload.get("funds", {}).get(fund.fund_number),
            )
            if values is not None:
                used_by_project[fund.project_id] += values["combined_total"]

    summaries = {}
    for project_id, project in projects.items():
        budget = project.budget_total
        used = used_by_project[project_id]
        utilization = (
            (used / budget * Decimal("100")).quantize(Decimal("0.01"))
            if budget
            else None
        )
        summaries[project_id] = {
            "budget": budget,
            "used": used,
            "utilization": utilization,
            "is_over_budget": utilization is not None and utilization > 100,
            "time_percentage": _project_time_percentage(project, today),
        }
        summaries[project_id]["utilization_bar_width"] = format(
            min(max(utilization or Decimal("0"), Decimal("0")), Decimal("100")),
            "f",
        )
        summaries[project_id]["time_bar_width"] = format(
            summaries[project_id]["time_percentage"],
            "f",
        )
    return summaries


@staff_member_required
def overview(request, year=None):
    _ensure_enabled()
    years = available_years(settings.SAP_DATA_DIR)
    selected_year = year if year is not None else (years[0] if years else None)
    payload = None
    cache_error = None
    if selected_year is not None:
        try:
            payload = load_year(settings.SAP_DATA_DIR, selected_year)
        except SAPCacheError as error:
            cache_error = str(error)

    payloads = {}
    for available_year in years:
        if available_year == selected_year and payload is not None:
            payloads[available_year] = payload
            continue
        try:
            payloads[available_year] = load_year(
                settings.SAP_DATA_DIR,
                available_year,
            )
        except SAPCacheError:
            # A damaged historical cache must not hide the selected year. It is
            # omitted from the lifetime calculation until it is rebuilt.
            continue

    cached_funds = payload.get("funds", {}) if payload else {}
    rows = []
    funds = list(
        SAPFund.objects.filter(is_active=True)
        .select_related("project", "annual_pool")
        .order_by("fund_number")
    )
    lifetime_summaries = _project_lifetime_summaries(funds, payloads)
    for fund in funds:
        owner, owner_type = _owner(fund)
        values = _display_values(fund, cached_funds.get(fund.fund_number))
        if not _has_year_data(values):
            continue
        is_adjusted = fund.treat_negative_actuals_as_funding
        rows.append(
            {
                "fund": fund,
                "owner": owner,
                "owner_type": owner_type,
                "values": values,
                "is_adjusted": is_adjusted,
                "lifetime": lifetime_summaries.get(fund.project_id),
            }
        )

    return render(
        request,
        "sap_integration/overview.html",
        {
            "years": years,
            "selected_year": selected_year,
            "rows": rows,
            "cache_error": cache_error,
            "generated_at": _generated_at(payload),
        },
    )


@staff_member_required
def fund_detail(request, year, fund_id):
    _ensure_enabled()
    fund = get_object_or_404(
        SAPFund.objects.select_related("project", "annual_pool"),
        pk=fund_id,
        is_active=True,
    )
    try:
        payload = load_year(settings.SAP_DATA_DIR, year)
    except SAPCacheError as error:
        raise Http404(str(error)) from error

    values = fund_values(payload.get("funds", {}).get(fund.fund_number))
    if not _has_year_data(values):
        raise Http404(f"Für Fonds {fund.fund_number} liegen {year} keine SAP-Daten vor.")
    owner, owner_type = _owner(fund)
    is_clean = request.GET.get("clean") == "1"
    if is_clean:
        values = clean_fund_values(
            values,
            treat_negative_actuals_as_funding=(
                fund.treat_negative_actuals_as_funding
            ),
        )
    return render(
        request,
        "sap_integration/fund_detail.html",
        {
            "fund": fund,
            "owner": owner,
            "owner_type": owner_type,
            "year": year,
            "years": available_years(settings.SAP_DATA_DIR),
            "values": values,
            "is_clean": is_clean,
            "generated_at": _generated_at(payload),
        },
    )
