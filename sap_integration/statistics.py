"""Read-only statistics shared by the web page and MCP, independent of planning links."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings

from projects.models import SAPFund
from sap_integration.cache import SAPCacheError, available_years, load_year
from sap_integration.cleaning import _without_counter_bookings
from sap_integration.config import financial_positions_enabled
from sap_integration.crosscheck import sap_actuals_by_year as gm_actuals_by_year
from sap_integration.salary_sync import SALARY_POSITION_PATTERN


@dataclass
class SAPStatistics:
    actuals: dict
    sources: list
    notes: list


def sap_statistics():
    """Project actuals only; commitments and funding receipts are not expenses.

    Würzburg's per-year cache is a snapshot, not an incremental import. Read
    original rows so grouping cannot move an amount into another calendar year.
    Keep signed corrections. For funds explicitly configured to treat negative
    actuals as funding, respect that convention as on the account statement.
    Where both adapters cover a fund/year, prefer the financial-position cache.
    """
    totals = defaultdict(lambda: Decimal("0.00"))
    covered = set()
    sources = []
    notes = []

    if financial_positions_enabled():
        sources.append("Finanzpositions-Exporte (WebGUI)")
        funds = list(SAPFund.objects.filter(is_active=True, project__isnull=False))
        found_data = False
        for export_year in available_years(settings.SAP_DATA_DIR):
            try:
                payload = load_year(settings.SAP_DATA_DIR, export_year)
            except (SAPCacheError, OSError):
                notes.append(f"SAP-Daten für {export_year} konnten nicht gelesen werden. Bitte parse_sap für dieses Jahr ausführen.")
                continue
            invalid_dates = 0
            invalid_amounts = 0
            for fund in funds:
                values = payload.get("funds", {}).get(fund.fund_number)
                if not values or not (values.get("has_budget") or values.get("transactions")):
                    continue
                found_data = True
                year = str(export_year)
                covered.add((fund.pk, year))
                # A valid snapshot with only commitments has known actuals of 0.
                totals[year] += Decimal("0.00")
                rows = []
                for row in values.get("transactions", []):
                    if row.get("type") != "actual":
                        continue
                    # Income codes are also excluded by the expense reconciliation.
                    if str(row.get("financial_position") or "").startswith("28"):
                        continue
                    try:
                        amount = Decimal(str(row["amount"]))
                        if not amount.is_finite():
                            raise InvalidOperation
                    except (KeyError, InvalidOperation, ValueError):
                        invalid_amounts += 1
                        continue
                    rows.append({**row, "amount": amount})
                if fund.treat_negative_actuals_as_funding:
                    # Match the account statement's funding convention, retaining
                    # individual dates instead of grouping remaining transactions.
                    rows = _without_counter_bookings(rows)
                for row in rows:
                    amount = row["amount"]
                    if fund.treat_negative_actuals_as_funding and amount < 0:
                        continue
                    text = " ".join(str(row.get(key) or "") for key in ("position", "header_text", "item_text"))
                    salary = SALARY_POSITION_PATTERN.search(text)
                    if salary:
                        year = salary.group(2)
                    else:
                        try:
                            year = str(date.fromisoformat(row.get("booking_date") or "").year)
                        except (TypeError, ValueError):
                            year = str(export_year)
                            invalid_dates += 1
                    covered.add((fund.pk, year))
                    totals[year] += amount
            if invalid_dates:
                notes.append(f"{export_year}: {invalid_dates} Ist-Buchung(en) ohne gültiges Buchungsdatum wurden dem Exportjahr zugeordnet.")
            if invalid_amounts:
                notes.append(f"{export_year}: {invalid_amounts} Ist-Buchung(en) mit ungültigem Betrag fehlen in der Summe.")
        if not found_data:
            notes.append("Für aktive, einem Projekt zugeordnete Fonds liegen noch keine aufbereiteten Finanzpositions-Exporte vor. Vorhandene Downloads lassen sich mit parse_sap aufbereiten.")

    if settings.SAP_GM_IMPORT_ENABLED:
        sources.append("Projektexporte (GM)")
        for year, amount in gm_actuals_by_year(excluded_fund_years=covered).items():
            totals[year] += amount
        if covered:
            notes.append("Bei Daten aus beiden Verfahren haben Finanzpositions-Exporte für denselben Fonds und dasselbe Jahr Vorrang.")

    return SAPStatistics(
        actuals={year: amount.quantize(Decimal("0.01")) for year, amount in totals.items()},
        sources=sources,
        notes=notes,
    )
