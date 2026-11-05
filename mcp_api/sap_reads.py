"""Read-only SAP adapters for MCP. No downloads, imports or planner writes."""

from django.conf import settings
from django.core.exceptions import ValidationError

from projects.models import SAPFund
from sap_integration.cache import SAPCacheError, available_years, fund_values, load_year
from sap_integration.category_rules import classify
from sap_integration.config import financial_positions_enabled

from .tools import ToolError, _money, _project


def enabled_sources():
    sources = []
    if financial_positions_enabled():
        sources.append("wuerzburg")
    if settings.SAP_GM_IMPORT_ENABLED:
        sources.append("gm")
    return sources


def _funds(acronym=None, fund_number=None):
    funds = SAPFund.objects.filter(is_active=True).select_related("project", "annual_pool").order_by("fund_number")
    if acronym:
        funds = funds.filter(project=_project(acronym))
    if fund_number:
        funds = funds.filter(fund_number=str(fund_number).strip())
    result = list(funds)
    if not result:
        raise ToolError("Für diese Auswahl ist kein aktiver SAP-Fonds konfiguriert. Fondszuordnung im Planner prüfen.")
    return result


def _fund_data(fund):
    return {"fund_number": fund.fund_number, "label": fund.label,
            "project": fund.project.acronym if fund.project_id else None,
            "annual_pool": fund.annual_pool.title if fund.annual_pool_id else None,
            "is_universal": fund.is_universal}


def _payload(year, funds):
    years = available_years(settings.SAP_DATA_DIR)
    if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100):
        raise ToolError("year: Geschäftsjahr als ganze Zahl zwischen 2000 und 2100 angeben.")
    if year is not None:
        try:
            return load_year(settings.SAP_DATA_DIR, year), years
        except (SAPCacheError, OSError) as error:
            raise ToolError(f"{error} Vorhandene Rohdateien mit parse_sap --year {year} aufbereiten; kein GM-Projektexport erforderlich.") from error
    if not years:
        raise ToolError("Noch keine aufbereiteten WebGUI-SAP-Daten vorhanden. Vorhandene Rohdateien mit parse_sap aufbereiten. Kein GM-Projektexport erforderlich.")
    latest = None
    errors = []
    for candidate in years:
        try:
            payload = load_year(settings.SAP_DATA_DIR, candidate)
        except (SAPCacheError, OSError) as error:
            errors.append(str(error))
            continue
        latest = latest or payload
        if any((values := payload.get("funds", {}).get(fund.fund_number)) and
               (values.get("has_budget") or values.get("transactions")) for fund in funds):
            return payload, years
    if latest is None:
        raise ToolError(" ".join(errors))
    return latest, years


def status(acronym=None):
    sources = enabled_sources()
    funds = _funds(acronym) if acronym else list(SAPFund.objects.filter(is_active=True).select_related("project", "annual_pool").order_by("fund_number"))
    return {
        "enabled_sources": sources,
        "default_source": sources[0] if len(sources) == 1 else None,
        "wuerzburg": {"enabled": "wuerzburg" in sources,
                      "available_years": available_years(settings.SAP_DATA_DIR) if "wuerzburg" in sources else [],
                      "read_tool": "get_sap_statement", "data": "Letzter aufbereiteter WebGUI-Download; kein Live-Zugriff."},
        "gm": {"enabled": "gm" in sources, "read_tool": "get_sap_reconciliation",
               "data": "Importierte GM-Projektexporte (GM_E_4GBA)."},
        "funds": [_fund_data(fund) for fund in funds],
    }


def statement(acronym=None, fund_number=None, year=None, transaction_type="all", financial_position=None, limit=200, offset=0):
    if not financial_positions_enabled():
        raise ToolError("Der WebGUI-Zugang nach Finanzpositionen ist nicht aktiv. get_sap_status zeigt die konfigurierten SAP-Verfahren.")
    if transaction_type not in {"all", "actual", "commitment"}:
        raise ToolError("transaction_type: all, actual oder commitment angeben.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ToolError("limit: ganze Zahl zwischen 1 und 500 angeben.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ToolError("offset: nichtnegative ganze Zahl angeben.")
    if financial_position is not None and not isinstance(financial_position, str):
        raise ToolError("financial_position: Finanzpositionsnummer als Text angeben.")
    from sap_integration.cleaning import clean_fund_values
    from sap_integration.models import SAPExpenseCategory
    from sap_integration.parser import DOCUMENT_COLUMNS

    funds = _funds(acronym, fund_number)
    payload, years = _payload(year, funds)
    categories = list(SAPExpenseCategory.objects.all())
    summaries, rows = [], []
    for fund in funds:
        values = fund_values(payload.get("funds", {}).get(fund.fund_number))
        has_data = bool(values and (values.get("has_budget") or values.get("transactions")))
        summary = {**_fund_data(fund), "has_data": has_data, "has_budget": bool(values and values.get("has_budget"))}
        for name in ("budget", "actual_total", "commitments_total", "combined_total", "remaining"):
            summary[name] = _money(values.get(name)) if has_data else None
        if values and fund.treat_negative_actuals_as_funding:
            adjusted = clean_fund_values(values, treat_negative_actuals_as_funding=True)
            summary["adjusted_display_totals"] = {key: _money(adjusted.get(key)) for key in
                                                  ("actual_total", "commitments_total", "combined_total", "remaining", "funding_total")}
        summaries.append(summary)
        for row in values.get("transactions", []) if values else []:
            if transaction_type != "all" and row["type"] != transaction_type:
                continue
            if financial_position is not None and row.get("financial_position") != financial_position.strip():
                continue
            category, matches = classify(row.get("financial_position", ""), categories)
            rows.append({"fund_number": fund.fund_number, "project": summary["project"],
                         **{key: row.get(key) for key in (*DOCUMENT_COLUMNS, "type", "business_partner", "position", "booking_date", "source_row")},
                         "amount": _money(row["amount"]),
                         "category": {"key": category.key, "label": category.label, "kind": category.kind} if category else None,
                         "category_conflicts": [c.label for c in matches] if len(matches) > 1 else []})
    return {"source": "wuerzburg", "view": "original", "year": payload["year"], "available_years": years,
            "generated_at": payload.get("generated_at"), "document_fields_available": payload["schema_version"] >= 2,
            "notice": None if payload["schema_version"] >= 2 else f"Für Finanzpositionen und Belegreferenzen parse_sap --year {payload['year']} erneut ausführen.",
            "funds": summaries, "transactions": rows[offset:offset + limit], "total_transactions": len(rows),
            "limit": limit, "offset": offset, "has_more": offset + limit < len(rows)}


def reconciliation(acronym, source="auto", year=None):
    if source not in {"auto", "wuerzburg", "gm"}:
        raise ToolError("source: auto, wuerzburg oder gm angeben.")
    sources = enabled_sources() if source == "auto" else [source]
    if not sources:
        raise ToolError("Keine SAP-Integration aktiv. get_sap_status zeigt den aktuellen Datenzugang.")
    if any(item not in enabled_sources() for item in sources):
        raise ToolError(f"SAP-Verfahren {source} ist deaktiviert. get_sap_status zeigt den aktiven Zugang.")
    project = _project(acronym)
    results, errors = [], []
    if "gm" in sources:
        from sap_integration.crosscheck import build_reconciliation

        for fund in project.sap_funds.all():
            result = build_reconciliation(fund)
            if result is None:
                continue
            results.append({"source": "gm", "psp_element": fund.fund_number,
                            "export_until": result.sap_import.last_booking.isoformat() if result.sap_import.last_booking else None,
                            "open_positions": [{"sap_reference": c.position.reference, "kind": c.position.get_kind_display(),
                                                "title": c.position.person_name or c.position.title, "status": c.status_label,
                                                "actual": _money(c.position.actual), "commitment": _money(c.position.commitment),
                                                "findings": c.findings} for c in result.open_checks]})
    if "wuerzburg" in sources:
        from sap_integration.expenses import build_expense_checks

        try:
            funds = _funds(acronym)
            payload, _ = _payload(year, funds)
            for fund in funds:
                entry = {"source": "wuerzburg", **_fund_data(fund), "year": payload["year"],
                         "generated_at": payload.get("generated_at"), "scope": "travel_and_other_expenses",
                         "open_positions": []}
                try:
                    checks = build_expense_checks(settings.SAP_DATA_DIR, fund, payload["year"])
                    entry["open_positions"] = [
                        {"source_key": c.key, "sap_reference": c.reference, "document_number": c.row.get("document_number"),
                         "financial_position": c.row.get("financial_position"), "kind": c.category.kind if c.category else None,
                         "category": c.category.label if c.category else None, "title": c.description,
                         "booking_date": c.row.get("booking_date"), "status": c.status,
                         "actual": _money(c.amount) if c.source_type == "actual" else 0.0,
                         "commitment": _money(c.amount) if c.source_type == "commitment" else 0.0,
                         "planning_transaction_id": c.link.planner_transaction_id if c.link else None,
                         "budget_item_id": c.budget_item.pk if c.budget_item else None,
                         "findings": [*c.blocking, *c.notes]} for c in checks if c.is_open]
                except (ValidationError, SAPCacheError, OSError) as error:
                    entry["error"] = " ".join(error.messages) if isinstance(error, ValidationError) else str(error)
                results.append(entry)
        except ToolError as error:
            errors.append({"source": "wuerzburg", "message": str(error)})
    if not results:
        if errors:
            raise ToolError(errors[0]["message"])
        raise ToolError(f"Für {project.acronym} liegt kein GM-Projektexport vor.")
    return {"project": project.acronym, "sources": sources, "funds": results, "errors": errors}
