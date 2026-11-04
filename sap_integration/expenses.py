"""Read-only Würzburg reconciliation and explicitly confirmed planning actions.

No GM models/transform functions are used. Cache rows are not deduplicated or
netted by amount. The identity is a document component, only actionable when
the export contains exactly one such component. Missing line numbers must not
be substituted with row indices, amounts, dates or description hashes.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import re

from django.core.exceptions import ValidationError
from django.db import transaction

from projects.models import OtherBudgetItemTransaction, SAPFund
from sap_integration.cache import available_years, load_year
from sap_integration.category_rules import classify, digest
from sap_integration.models import (
    SAPExpenseAudit, SAPExpenseBudgetMapping, SAPExpenseCategory, SAPExpenseLink,
)
from sap_integration.parser import DOCUMENT_COLUMNS


CENT = Decimal("0.01")
IDENTITY_FIELDS = (
    "type", "financial_center", "document_number", "financial_position",
    "gl_account", "purchase_document", "document_item",
)
SNAPSHOT_FIELDS = (*DOCUMENT_COLUMNS, "type", "business_partner", "position", "amount", "booking_date")
PAYROLL = re.compile(r"\bGehalt\s+\w+\s+20\d\d|\bMittelbindung\s+von\b", re.I)


def source_row(row):
    return {key: str(row.get(key) or "") for key in SNAPSHOT_FIELDS}


def transaction_state(entry):
    if entry is None:
        return None
    return {"id": entry.pk, "budget_item": entry.budget_item_id, "date": entry.date.isoformat(),
            "amount": str(entry.amount), "description": entry.description or "", "sap_id": entry.sap_id or ""}


@dataclass
class ExpenseCheck:
    fund: object
    year: int
    key: str
    rows: list
    category: object = None
    budget_item: object = None
    link: object = None
    blocking: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    missing: bool = False

    @property
    def row(self):
        return self.rows[0] if self.rows else {}

    @property
    def source_type(self):
        return self.row.get("type", "actual")

    @property
    def source_label(self):
        return "SAP-Ist" if self.source_type == "actual" else "Offenes SAP-Obligo"

    @property
    def amount(self):
        return sum((Decimal(row["amount"]) for row in self.rows), Decimal("0.00"))

    @property
    def booking_date(self):
        try:
            return date.fromisoformat(self.row.get("booking_date") or "")
        except ValueError:
            return None

    @property
    def description(self):
        return self.row.get("position", "") or self.row.get("business_partner", "")

    @property
    def reference(self):
        return f"WUE:{self.year}:{self.fund.fund_number}:{self.row.get('document_number', '')}"

    @property
    def snapshot(self):
        return {"rows": sorted((source_row(row) for row in self.rows), key=digest)}

    @property
    def can_apply(self):
        return not self.blocking and not self.missing and self.category is not None and (
            self.link is None or self.link.is_active
        ) and (self.amount != 0 or self.link is not None)

    @property
    def status(self):
        if self.link and not self.link.is_active:
            return "Abgelöstes Obligo"
        if self.missing:
            return "Nicht mehr im Export"
        if self.blocking:
            return "Prüfung erforderlich"
        if not self.category:
            return "Kategorie fehlt"
        if not self.link:
            return "Ohne Betrag" if self.amount == 0 else "Fehlt in Planung"
        target = self.link.planner_transaction
        if (target.amount != self.amount or target.date != self.booking_date
                or (target.description or "") != self.description
                or (self.budget_item and target.budget_item_id != self.budget_item.pk)
                or self.link.source_snapshot != self.snapshot):
            return "Abweichung"
        return "Übereinstimmend"

    @property
    def is_open(self):
        return self.status not in {"Übereinstimmend", "Abgelöstes Obligo", "Ohne Betrag"}


def build_expense_checks(data_dir, fund, year):
    payload = load_year(data_dir, year)
    if payload["schema_version"] < 2:
        raise ValidationError(f"Bitte die vorhandenen Rohdateien für {year} mit parse_sap --year {year} erneut aufbereiten.")
    categories = list(SAPExpenseCategory.objects.all())
    mappings = {m.category_id: m.budget_item for m in SAPExpenseBudgetMapping.objects.filter(fund=fund).select_related("budget_item")}
    links = {link.source_key: link for link in SAPExpenseLink.objects.filter(fund=fund, year=year).select_related(
        "planner_transaction__budget_item", "remainder_transaction"
    )}
    groups = {}
    for row in payload.get("funds", {}).get(fund.fund_number, {}).get("transactions", []):
        code = row.get("financial_position", "")
        category, _ = classify(code, categories)
        # Personnel remains exclusively in the existing Würzburg salary path.
        if (category and category.kind == "staff") or code.startswith(("429", "28")) or PAYROLL.search(row.get("position", "")):
            continue
        identity = {key: row.get(key, "") for key in IDENTITY_FIELDS}
        key = digest({"backend": "wuerzburg", "fund": fund.fund_number, "year": year, **identity})
        groups.setdefault(key, []).append(row)

    checks = []
    for key, rows in groups.items():
        check = ExpenseCheck(fund=fund, year=year, key=key, rows=rows, link=links.pop(key, None))
        check.category, matches = classify(check.row.get("financial_position", ""), categories)
        if len(matches) > 1:
            check.blocking.append("Mehrere Kategorien treffen zu: " + ", ".join(c.label for c in matches))
        if check.category:
            check.budget_item = mappings.get(check.category.pk)
            if not check.budget_item:
                check.notes.append("Für diese Kategorie ist noch kein Planungsbudget am Fonds hinterlegt.")
        if not fund.project_id:
            check.blocking.append("Für die Übernahme muss der Fonds einem Projekt zugeordnet sein.")
        if len(rows) != 1:
            check.blocking.append(
                f"{len(rows)} Exportzeilen teilen diesen Belegschlüssel. Eine eindeutige Belegposition fehlt; keine automatische Zusammenfassung."
            )
        if not all(check.row.get(k) for k in ("financial_center", "document_number", "financial_position", "gl_account")):
            check.blocking.append("Finanzstelle, FI-Belegnummer, Finanzposition oder Sachkonto fehlt.")
        if check.source_type not in {"actual", "commitment"} or not check.booking_date:
            check.blocking.append("Buchungstyp oder Buchungsdatum ist ungültig.")
        if check.booking_date and fund.project_id and check.booking_date.year not in {int(y) for y in fund.project.get_years()}:
            check.blocking.append("Das Buchungsjahr liegt außerhalb der Projektjahre und würde in der Planung nicht mitgezählt. Bitte zuerst die Projektlaufzeit prüfen.")
        if check.amount < 0:
            check.notes.append("Der negative SAP-Betrag wird mit seinem Vorzeichen übernommen und vermindert die geplanten Ausgaben.")
        if check.source_type == "commitment":
            check.notes.append("Dies ist eine Vormerkung in SAP. Wenn die Zahlung vorliegt, wähle für den Abgleich denselben Planungseintrag aus.")
        if check.link and check.link.planner_transaction.budget_item.project_id != fund.project_id:
            check.blocking.append("Das Projekt der gespeicherten Verknüpfung passt nicht mehr zum Fonds.")
        checks.append(check)
    for key, link in links.items():
        if link.is_active:
            old_documents = {row.get("document_number") for row in link.source_snapshot.get("rows", [])}
            for check in checks:
                if check.link is None and check.source_type == link.source_type and check.row.get("document_number") in old_documents:
                    check.blocking.append("Zu diesem FI-Beleg fehlt eine frühere Verknüpfung im Export. Mögliche Umbuchung: erst die bestehende Planung prüfen.")
            checks.append(ExpenseCheck(fund=fund, year=year, key=key,
                                      rows=link.source_snapshot.get("rows", []), link=link, missing=True,
                                      blocking=["Die verknüpfte Buchung fehlt im aktuellen Export. Keine automatische Löschung oder Ausbuchung."]))
    return sorted(checks, key=lambda check: (check.row.get("booking_date") or "", check.key), reverse=True)


def expense_warnings(data_dir):
    """Summary warnings; page reads never mutate planning or source data."""
    from django.urls import reverse

    warnings = []
    years = available_years(data_dir)
    for fund in SAPFund.objects.filter(is_active=True, project__isnull=False).select_related("project"):
        for year in years:
            try:
                checks = [c for c in build_expense_checks(data_dir, fund, year) if c.is_open]
            except (ValueError, ValidationError, OSError) as error:
                message = " ".join(error.messages) if isinstance(error, ValidationError) else str(error)
                warnings.append({
                    "severity": "warning", "title": f"SAP-Ausgabenabgleich nicht möglich: {fund.project.acronym} / {fund.fund_number} / {year}",
                    "detail": message, "link": reverse("sap_integration:expenses", args=[fund.pk]) + f"?year={year}",
                })
                continue
            if not checks:
                continue
            missing = sum(c.link is None for c in checks)
            details = [
                f"Beleg {c.row.get('document_number') or 'ohne Nummer'} · {c.row.get('booking_date') or 'ohne Datum'} · "
                f"Finanzposition {c.row.get('financial_position') or 'fehlt'} · {c.source_label} {c.amount:.2f} EUR · "
                f"{'Nicht in Planung verknüpft' if c.link is None else c.status}: {c.description}"
                for c in checks
            ]
            warnings.append({
                "severity": "warning", "title": f"SAP-Ausgabenabgleich: {fund.project.acronym} / {fund.fund_number} / {year}",
                "detail": f"{missing} SAP-Buchung(en) ohne verknüpften Planungseintrag, {len(checks) - missing} weitere Abweichung(en) bei Reisen und Sachmitteln.",
                "details": details, "collapse_details": True,
                "link": reverse("sap_integration:expenses", args=[fund.pk]) + f"?year={year}",
            })
    return warnings


def expense_planning_annotations(project):
    from django.urls import reverse

    annotations = {}
    for link in SAPExpenseLink.objects.filter(is_active=True, planner_transaction__budget_item__project=project).select_related("fund"):
        rows = link.source_snapshot.get("rows", [])
        annotations[link.planner_transaction_id] = {
            "status": "ist" if link.source_type == "actual" else "obligo",
            "actual": sum((Decimal(row["amount"]) for row in rows), Decimal("0.00")) if link.source_type == "actual" else Decimal("0.00"),
            "reference": f"WUE:{link.year}:{link.fund.fund_number}:{rows[0].get('document_number', '')}" if rows else "Würzburg",
            "url": reverse("sap_integration:expense_detail", args=[link.fund_id, link.year, link.source_key]),
        }
    return annotations


def preview_expense(check, data):
    if not check.can_apply:
        raise ValidationError("Diese Buchung kann nicht sicher übernommen werden. Bitte zunächst die offenen Zuordnungen klären.")
    target = data.get("planner_transaction")
    budget = target.budget_item if target else data.get("budget_item")
    if budget is None:
        raise ValidationError("Bitte für den neuen Eintrag ein Planungsbudget auswählen.")
    if budget.project_id != check.fund.project_id or (target and target.budget_item.project_id != check.fund.project_id):
        raise ValidationError("Planung und Budget müssen zum Projekt des Fonds gehören.")
    if check.link and target != check.link.planner_transaction:
        raise ValidationError("Diese SAP-Buchung ist bereits mit einem anderen Planungseintrag verknüpft.")
    existing_link = SAPExpenseLink.objects.filter(planner_transaction=target, is_active=True).first() if target else None
    replacement = existing_link if existing_link and (not check.link or existing_link.pk != check.link.pk) else None
    if replacement and not (replacement.source_type == "commitment" and check.source_type == "actual"):
        raise ValidationError("Der Planungseintrag ist bereits mit einer anderen SAP-Buchung verknüpft. Bitte einen anderen Eintrag wählen.")
    if replacement and replacement.fund_id != check.fund.pk:
        raise ValidationError("Obligo und Ist müssen zum selben Fonds gehören.")
    if target and target.sap_id and not existing_link:
        raise ValidationError("Der Eintrag besitzt bereits eine SAP-Referenz. Bitte zuerst die bestehende Verknüpfung prüfen (z. B. GM).")
    expected_reference = check.reference
    if replacement:
        old_rows = replacement.source_snapshot.get("rows", [])
        old_document = old_rows[0].get("document_number", "") if old_rows else ""
        expected_reference = f"WUE:{replacement.year}:{check.fund.fund_number}:{old_document}"
    if target and existing_link and target.sap_id and target.sap_id != expected_reference:
        raise ValidationError("Die SAP-Referenz des Planungseintrags wurde verändert. Bitte die bestehende Verknüpfung prüfen.")
    if check.amount < 0 and target and target.amount > 0 and not check.link:
        raise ValidationError("Eine Gutschrift darf eine positive Ausgabe nicht ersetzen. Bitte einen eigenen Korrektureintrag anlegen.")
    remainder = check.link.remainder_transaction if check.link else None
    # Retain historical records from earlier versions, without silently
    # changing a separately planned or already paid expense.
    if remainder and (remainder.amount or SAPExpenseLink.objects.filter(planner_transaction=remainder, is_active=True).exists()):
        raise ValidationError("Zu diesem Eintrag besteht eine zusätzliche Vorplanung aus einem früheren Abgleich. Bitte diese vor einer erneuten Übernahme prüfen.")
    if len(check.reference) > 100:
        raise ValidationError("Die SAP-Referenz ist für den Planungseintrag zu lang.")
    if not check.amount.is_finite() or abs(check.amount) > Decimal("99999999.99"):
        raise ValidationError("Der SAP-Betrag liegt außerhalb des Wertebereichs der Planung.")
    after = {
        "budget_item": budget.pk, "amount": str(check.amount), "date": check.booking_date.isoformat(),
        "description": check.description, "sap_id": check.reference,
    }
    before_total = target.amount if target else Decimal("0")
    after_total = check.amount
    return {"source": check.snapshot, "source_key": check.key,
            "before": transaction_state(target), "after": after,
            "total_before": str(before_total), "total_after": str(after_total), "delta": str(after_total - before_total),
            "budget_label": budget.title,
            "link_id": check.link.pk if check.link else None,
            "replacement": replacement.pk if replacement else None}


@transaction.atomic
def apply_expense(data_dir, fund, year, source_key, data, *, expected_preview, user):
    # Re-read both SAP and planner data after locking the fund. This also
    # serializes attempts to create the same source/target link on supported DBs.
    fund = SAPFund.objects.select_for_update().get(pk=fund.pk)
    check = next((c for c in build_expense_checks(data_dir, fund, year) if c.key == source_key), None)
    if check is None:
        raise ValidationError("Die SAP-Buchung wurde inzwischen geändert. Bitte den Abgleich neu öffnen.")
    if data.get("planner_transaction"):
        data = {**data, "planner_transaction": OtherBudgetItemTransaction.objects.select_for_update().get(pk=data["planner_transaction"].pk)}
    preview = preview_expense(check, data)
    if digest(preview) != digest(expected_preview):
        raise ValidationError("SAP-Daten, Verknüpfung oder Planung wurden inzwischen geändert. Bitte die Seite neu laden.")
    target = data.get("planner_transaction")
    if preview["replacement"]:
        SAPExpenseLink.objects.filter(pk=preview["replacement"]).update(is_active=False)
    if target is None:
        target = OtherBudgetItemTransaction()
    target.budget_item_id = preview["after"]["budget_item"]
    target.amount = check.amount
    target.date = check.booking_date
    target.description = check.description
    target.sap_id = check.reference
    target.full_clean()
    target.save()
    link = check.link or SAPExpenseLink(fund=fund, year=year, source_key=check.key, source_type=check.source_type)
    link.planner_transaction = target
    link.source_snapshot = check.snapshot
    link.planning_snapshot = {"main": transaction_state(target), "remainder": transaction_state(link.remainder_transaction)}
    link.confirmed_by = user
    link.save()
    SAPExpenseAudit.objects.create(link=link, user=user,
                                   action="apply",
                                   before={"main": preview["before"],
                                           "replaced_link": preview["replacement"]},
                                   after=link.planning_snapshot, source_snapshot=check.snapshot)
    return link
