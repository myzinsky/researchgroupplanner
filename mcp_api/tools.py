"""Tools of the MCP endpoint: read access to the planning and a few staff-only write actions."""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """Error reported to the MCP client as a tool result with isError."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    function: object
    write: bool = False


TOOLS = {}


def tool(name, description, properties=None, required=(), write=False):
    def register(function):
        TOOLS[name] = Tool(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties or {}, "required": list(required)},
            function=function,
            write=write,
        )
        return function
    return register


def available_tools(user):
    """Tools the user may use: write tools only for staff users."""
    return [t for t in TOOLS.values() if not t.write or user.is_staff]


def call_tool(user, name, arguments):
    found = TOOLS.get(name)
    if found is None or (found.write and not user.is_staff):
        raise ToolError(f"Unbekanntes oder nicht erlaubtes Werkzeug: {name}")
    if found.write:
        logger.info("MCP write tool %s by %s: %s", name, user, arguments)
    return found.function(user=user, **(arguments or {}))


# --- helpers ---------------------------------------------------------------

def _money(value):
    return None if value is None or value == "" else float(Decimal(value).quantize(Decimal("0.01")))


def _day(value):
    return value.isoformat() if value else None


def _parse_day(value, field):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolError(f"{field}: Datum im Format JJJJ-MM-TT erwartet, erhalten: {value!r}")


def _project(acronym):
    from projects.models import Project

    project = Project.objects.filter(acronym__iexact=acronym).first()
    if project is None:
        raise ToolError(f"Projekt „{acronym}“ nicht gefunden.")
    return project


def _allocation_source(allocation):
    if allocation.budget_item_id:
        return f"{allocation.budget_item.project.acronym} – {allocation.budget_item.title}"
    if allocation.annual_pool_budget_id:
        return f"Annual Pool {allocation.annual_pool_budget.annual_pool.title} ({allocation.annual_pool_budget.year})"
    if allocation.is_universal:
        return "Universalprojekt"
    return f"Landesstelle {allocation.landesstelle.title}"


def _rebooking_data(rebooking):
    return {
        "rebooking_id": rebooking.id,
        "person": str(rebooking.allocation.employment.staff_member),
        "allocation_id": rebooking.allocation_id,
        "from": _allocation_source(rebooking.allocation),
        "from_psp": rebooking.source_psp,
        "to": f"{rebooking.budget_item.project.acronym} – {rebooking.budget_item.title}",
        "to_budget_item_id": rebooking.budget_item_id,
        "to_psp": rebooking.target_psp,
        "percentage": _money(rebooking.percentage),
        "start": _day(rebooking.start_date),
        "end": _day(rebooking.end),
    }


def _staff_member(staff_id=None, name=None):
    from staffing.models import StaffMember

    if staff_id:
        member = StaffMember.objects.filter(id=staff_id).first()
        if member is None:
            raise ToolError(f"Keine Person mit ID {staff_id}.")
        return member
    if not name:
        raise ToolError("Bitte staff_id oder name angeben.")
    terms = name.split()
    candidates = StaffMember.objects.all()
    for term in terms:
        candidates = candidates.filter(Q(first_name__icontains=term) | Q(last_name__icontains=term))
    candidates = list(candidates)
    if not candidates:
        raise ToolError(f"Keine Person zu „{name}“ gefunden.")
    if len(candidates) > 1:
        raise ToolError("Mehrdeutig, bitte staff_id verwenden: " + ", ".join(f"{m} (ID {m.id})" for m in candidates))
    return candidates[0]


# --- read tools ------------------------------------------------------------

@tool(
    "list_projects",
    "Projekte mit Laufzeit und Budgetnutzung (geplante Kosten ohne Overhead gegen Budget ohne Overhead). "
    "status: running (laufend/geplant, Standard), completed oder all.",
    {"status": {"type": "string", "enum": ["running", "completed", "all"]}},
)
def list_projects(user, status="running"):
    from projects.models import Project
    from projects.utils import project_budget_overview

    today = timezone.now().date()
    running = Q(extension_planning_date__isnull=False, extension_planning_date__gte=today) | Q(
        extension_planning_date__isnull=True, end_date__gte=today,
    )
    projects = Project.objects.order_by("start_date", "acronym")
    if status == "running":
        projects = projects.filter(running)
    elif status == "completed":
        projects = projects.exclude(running)
    projects, totals = project_budget_overview(projects)
    return {
        "projects": [
            {
                "acronym": p.acronym,
                "start": _day(p.start_date),
                "end": _day(p.get_effective_end_date()),
                "budget_total": _money(p.budget_total),
                "budget_without_overhead": _money(p.usage_budget),
                "planned_costs": _money(p.used_budget),
                "usage_percent": _money(p.usage_percent),
                "planned_costs_with_open_rebookings": _money(getattr(p, "rebooked_used", None)),
            }
            for p in projects
        ],
        "totals": {
            "budget_without_overhead": _money(totals["budget"]),
            "planned_costs": _money(totals["used"]),
            "usage_percent": _money(totals["usage_percent"]),
        },
    }


@tool(
    "get_project",
    "Details eines Projekts: Budgetposten mit Nutzung und Personenmonaten, Personal-Zuordnungen, offene "
    "Umbuchungen, Deliverables, Meilensteine, Berichtszeiträume, Links und Ansprechpersonen.",
    {"acronym": {"type": "string", "description": "Projektkürzel, z. B. ISOLDE"}},
    required=["acronym"],
)
def get_project(user, acronym):
    from projects.models import Project
    from projects.utils import get_allocation_person_months, project_budget_overview
    from staffing.models import Rebooking, StaffFundingAllocation

    project = _project(acronym)
    (project,), _ = project_budget_overview(Project.objects.filter(pk=project.pk))
    allocations = list(
        StaffFundingAllocation.objects.filter(budget_item__project=project)
        .select_related("employment__staff_member", "budget_item")
        .order_by("start_date")
    )
    rebookings = Rebooking.objects.filter(
        Q(budget_item__project=project) | Q(allocation__budget_item__project=project)
    ).select_related("allocation__employment__staff_member", "allocation__budget_item__project", "budget_item__project")
    return {
        "acronym": project.acronym,
        "start": _day(project.start_date),
        "end": _day(project.end_date),
        "extension_until": _day(project.extension_planning_date),
        "budget_total": _money(project.budget_total),
        "overhead": _money(sum((i.amount for i in project.overheadbudgetitem_set.all()), Decimal("0"))),
        "planned_costs": _money(project.used_budget),
        "usage_percent": _money(project.usage_percent),
        "staff_budget_items": [
            {
                "budget_item_id": item.id,
                "title": item.title,
                "amount": _money(item.amount),
                "planned": _money(item.used),
                "remaining": _money(item.amount - item.used),
                "planned_with_open_rebookings": _money(getattr(item, "rebooked_used", None)),
                "person_months": _money(sum(
                    (sum(get_allocation_person_months(a).values(), Decimal("0")) for a in allocations
                     if a.budget_item_id == item.id),
                    Decimal("0"),
                )),
            }
            for item in project.staffbudgetitem_set.all()
        ],
        "other_budget_items": [
            {"title": item.title, "amount": _money(item.amount), "planned": _money(item.used),
             "remaining": _money(item.amount - item.used)}
            for item in project.otherbudgetitem_set.all()
        ],
        "staff_allocations": [
            {
                "allocation_id": a.id,
                "person": str(a.employment.staff_member),
                "staff_id": a.employment.staff_member_id,
                "employment_status": a.employment.get_status_display(),
                "budget_item": a.budget_item.title,
                "percentage": _money(a.percentage),
                "start": _day(a.start_date),
                "end": _day(a.end_date or a.employment.end_date),
                "sap_reference": a.sap_reference,
            }
            for a in allocations
        ],
        "open_rebookings": [_rebooking_data(r) for r in rebookings],
        "deliverables": [
            {"title": d.title, "deadline": _day(d.deadline), "lead": d.is_lead} for d in project.deliverables.all()
        ],
        "milestones": [{"title": m.title, "date": _day(m.date)} for m in project.milestones.all()],
        "reporting_periods": [
            {"title": r.title, "start": _day(r.start_date), "end": _day(r.end_date)}
            for r in project.reporting_periods.all()
        ],
        "links": [{"title": link.title, "url": link.url} for link in project.links.order_by("title")],
        "contacts": [
            {"function": c.function, "name": c.name, "email": c.email} for c in project.contacts.order_by("function", "name")
        ],
    }


@tool(
    "list_budget_items",
    "Personalbudgets (IDs für Umbuchungen und Anstellungsplanung), optional nur eines Projekts.",
    {"acronym": {"type": "string"}},
)
def list_budget_items(user, acronym=None):
    from projects.models import StaffBudgetItem

    items = StaffBudgetItem.objects.select_related("project").order_by("project__acronym", "title")
    if acronym:
        items = items.filter(project=_project(acronym))
    return {"staff_budget_items": [
        {"budget_item_id": i.id, "project": i.project.acronym, "title": i.title, "amount": _money(i.amount)}
        for i in items
    ]}


@tool(
    "list_staff",
    "Mitarbeitende mit aktueller bzw. nächster Anstellung. include_former: auch ehemalige.",
    {"include_former": {"type": "boolean"}},
)
def list_staff(user, include_former=False):
    from staffing.models import StaffMember

    today = timezone.now().date()
    result = []
    for member in StaffMember.objects.prefetch_related("employment_set").order_by("last_name", "first_name"):
        employments = sorted(member.employment_set.all(), key=lambda e: e.start_date)
        current = next((e for e in employments if e.end_date >= today), None)
        if current is None and not include_former:
            continue
        result.append({
            "staff_id": member.id,
            "name": str(member),
            "status": member.get_status_display(),
            "employment": {
                "category": current.get_category(), "percentage": _money(current.percentage),
                "start": _day(current.start_date), "end": _day(current.end_date),
                "status": current.get_status_display(),
            } if current else None,
        })
    return {"staff": result}


@tool(
    "get_staff_member",
    "Details einer Person: Anstellungen (Entgeltgruppe, Stufe), Zuordnungen mit IDs, offene Umbuchungen, "
    "Monatsgehälter (Planung und SAP-Ist) und SAP-Mittelreservierungen mit Hinweisen.",
    {"staff_id": {"type": "integer"}, "name": {"type": "string", "description": "Vor- und/oder Nachname"}},
)
def get_staff_member(user, staff_id=None, name=None):
    from staffing.utils import get_salaries_by_month, get_sap_actuals_by_month, staff_reservations

    member = _staff_member(staff_id, name)
    employments = []
    for employment in member.employment_set.order_by("start_date"):
        allocations = list(employment.stafffundingallocation_set.select_related(
            "budget_item__project", "landesstelle", "annual_pool_budget__annual_pool",
        ).order_by("start_date"))
        planned = get_salaries_by_month(employment)
        actuals = get_sap_actuals_by_month(allocations)
        employments.append({
            "employment_id": employment.id,
            "start": _day(employment.start_date),
            "end": _day(employment.end_date),
            "percentage": _money(employment.percentage),
            "category": employment.get_category(),
            "status": employment.get_status_display(),
            "salary_category": str(employment.salary_category) if employment.salary_category else None,
            "start_level": employment.start_level,
            "level_start_date": _day(employment.level_start_date),
            "statutory_health_insurance": employment.statutory_health_insurance,
            "allocations": [
                {
                    "allocation_id": a.id,
                    "source": _allocation_source(a),
                    "percentage": _money(a.percentage),
                    "start": _day(a.start_date),
                    "end": _day(a.end_date or employment.end_date),
                    "sap_reference": a.sap_reference,
                    "open_rebookings": [_rebooking_data(r) for r in a.rebookings.select_related("budget_item__project")],
                }
                for a in allocations
            ],
            "monthly_costs": {
                month: {
                    "planned": _money(amount),
                    "sap_actual": _money(sum((e["amount"] for e in actuals[month]), Decimal("0"))) if month in actuals else None,
                }
                for month, amount in planned.items()
            },
        })
    return {
        "staff_id": member.id,
        "name": str(member),
        "status": member.get_status_display(),
        "employments": employments,
        "sap_reservations": [
            {
                "project": r["project"].acronym,
                "sap_reference": r["position"].reference,
                "periods": [[_day(s), _day(e)] for s, e in r["periods"]],
                "percentage": _money(r["position"].percentage),
                "actual": _money(r["position"].actual),
                "open_commitment": _money(r["position"].commitment),
                "matched_by_name_only": not r["linked"],
                "notices": r["issues"],
            }
            for r in staff_reservations(member)
        ],
    }


@tool("list_rebookings", "Offene Umbuchungen mit IDs, Von/Nach (inkl. PSP-Elementen), Umfang und Zeitraum.")
def list_rebookings(user):
    from staffing.models import Rebooking

    rebookings = Rebooking.objects.select_related(
        "allocation__employment__staff_member", "allocation__budget_item__project", "budget_item__project",
    ).order_by("start_date")
    return {"open_rebookings": [_rebooking_data(r) for r in rebookings]}


@tool(
    "list_warnings",
    "Konsistenz-Warnungen der Planung (wie die Seite Warnungen) mit Schlüssel zum Ignorieren. "
    "include_ignored: auch ignorierte mit Begründung.",
    {"include_ignored": {"type": "boolean"}},
)
def list_warnings(user, include_ignored=False):
    from controlling.views import collect_warnings

    active, ignored = collect_warnings()

    def data(warning):
        entry = {
            "key": warning["key"],
            "severity": warning["severity"],
            "title": warning["title"],
            "details": warning.get("details") or [warning.get("detail", "")],
        }
        if warning.get("ignored"):
            entry["ignored_comment"] = warning["ignored"].comment
        return entry

    result = {"warnings": [data(w) for w in active]}
    if include_ignored:
        result["ignored_warnings"] = [data(w) for w in ignored]
    return result


@tool(
    "get_sap_reconciliation",
    "Offene Punkte des SAP-Abgleichs eines Projekts (fehlende oder abweichende Positionen mit Befunden).",
    {"acronym": {"type": "string"}},
    required=["acronym"],
)
def get_sap_reconciliation(user, acronym):
    from sap_integration.crosscheck import build_reconciliation

    project = _project(acronym)
    result = []
    for fund in project.sap_funds.all():
        reconciliation = build_reconciliation(fund)
        if reconciliation is None:
            continue
        result.append({
            "psp_element": fund.fund_number,
            "export_until": _day(reconciliation.sap_import.last_booking),
            "open_positions": [
                {
                    "sap_reference": c.position.reference,
                    "kind": c.position.get_kind_display(),
                    "title": c.position.person_name or c.position.title,
                    "status": c.status_label,
                    "actual": _money(c.position.actual),
                    "commitment": _money(c.position.commitment),
                    "findings": c.findings,
                }
                for c in reconciliation.open_checks
            ],
        })
    if not result:
        raise ToolError(f"Für {project.acronym} liegt kein SAP-Projektexport vor.")
    return {"project": project.acronym, "funds": result}


@tool(
    "list_deliverables",
    "Deliverables aller Projekte nach Deadline. days_ahead: nur die nächsten N Tage; include_past: auch vergangene.",
    {"days_ahead": {"type": "integer"}, "include_past": {"type": "boolean"}},
)
def list_deliverables(user, days_ahead=None, include_past=False):
    from projects.models import ProjectDeliverable

    today = timezone.now().date()
    deliverables = ProjectDeliverable.objects.select_related("project").order_by("deadline")
    if not include_past:
        deliverables = deliverables.filter(deadline__gte=today)
    if days_ahead is not None:
        deliverables = deliverables.filter(deadline__lte=today + timedelta(days=int(days_ahead)))
    return {"deliverables": [
        {"project": d.project.acronym, "title": d.title, "deadline": _day(d.deadline), "lead": d.is_lead}
        for d in deliverables
    ]}


@tool(
    "get_statistics",
    "Je Jahr: verteilte Drittmittel (inkl. Overhead), Overhead, geplante Kosten (ohne Overhead) und SAP-Ist.",
)
def get_statistics(user):
    from controlling.views import funds_by_year
    from projects.models import Project
    from projects.utils import planned_expenses_by_year
    from sap_integration.crosscheck import sap_actuals_by_year

    projects = list(Project.objects.prefetch_related("overheadbudgetitem_set"))
    funds = funds_by_year(projects)
    planned = planned_expenses_by_year(projects)
    actuals = sap_actuals_by_year()
    rows = []
    for year in sorted(set(funds) | set(planned) | set(actuals)):
        parts = planned.get(year, {})
        rows.append({
            "year": year,
            "funds": _money(funds.get(year, Decimal("0"))),
            "overhead": _money(parts.get("overhead", Decimal("0"))),
            "planned_costs": _money(parts.get("staff", Decimal("0")) + parts.get("other", Decimal("0"))),
            "sap_actual": _money(actuals.get(year)),
        })
    return {"total_funds": _money(sum((p.budget_total for p in projects), Decimal("0"))), "years": rows}


# --- write tools (staff only) ------------------------------------------------

@tool(
    "create_rebooking",
    "Legt eine offene Umbuchung an (ändert die Zuordnung nicht). allocation_id aus get_staff_member/get_project, "
    "budget_item_id aus list_budget_items; end_date leer = Ende der Zuordnung.",
    {
        "allocation_id": {"type": "integer"},
        "budget_item_id": {"type": "integer"},
        "percentage": {"type": "number"},
        "start_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "end_date": {"type": "string", "description": "JJJJ-MM-TT, optional"},
    },
    required=["allocation_id", "budget_item_id", "percentage", "start_date"],
    write=True,
)
def create_rebooking(user, allocation_id, budget_item_id, percentage, start_date, end_date=None):
    from projects.models import StaffBudgetItem
    from staffing.models import Rebooking, StaffFundingAllocation

    allocation = StaffFundingAllocation.objects.filter(id=allocation_id).first()
    budget_item = StaffBudgetItem.objects.filter(id=budget_item_id).first()
    if allocation is None or budget_item is None:
        raise ToolError("Zuordnung oder Personalbudget nicht gefunden.")
    rebooking = Rebooking(
        allocation=allocation, budget_item=budget_item, percentage=Decimal(str(percentage)),
        start_date=_parse_day(start_date, "start_date"), end_date=_parse_day(end_date, "end_date"),
    )
    try:
        rebooking.full_clean()
    except ValidationError as error:
        raise ToolError(" ".join(error.messages))
    rebooking.save()
    return {"created": _rebooking_data(rebooking)}


@tool(
    "complete_rebooking",
    "Schließt eine offene Umbuchung ab: die Zuordnung wird aufgeteilt und der Zeitraum auf das neue Budget übernommen.",
    {"rebooking_id": {"type": "integer"}},
    required=["rebooking_id"],
    write=True,
)
def complete_rebooking(user, rebooking_id):
    from staffing.models import Rebooking
    from staffing.models import complete_rebooking as apply_rebooking

    rebooking = Rebooking.objects.filter(id=rebooking_id).first()
    if rebooking is None:
        raise ToolError(f"Keine offene Umbuchung mit ID {rebooking_id}.")
    target = apply_rebooking(rebooking)
    return {"completed": True, "new_allocation_id": target.id, "source": _allocation_source(target)}


@tool(
    "delete_rebooking",
    "Löscht eine offene Umbuchung; die Zuordnung bleibt unverändert.",
    {"rebooking_id": {"type": "integer"}},
    required=["rebooking_id"],
    write=True,
)
def delete_rebooking(user, rebooking_id):
    from staffing.models import Rebooking

    deleted, _ = Rebooking.objects.filter(id=rebooking_id).delete()
    if not deleted:
        raise ToolError(f"Keine offene Umbuchung mit ID {rebooking_id}.")
    return {"deleted": True}


@tool(
    "plan_employment",
    "Plant eine Anstellung mit Zuordnung zu einem Personalbudget (wie „Anstellung planen“). Bestehende Person per "
    "staff_id oder neue per first_name/last_name. Mit salary_category (z. B. E13) und start_level werden die "
    "Gehälter aus den TV-L-Tabellen geschätzt.",
    {
        "staff_id": {"type": "integer"},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "budget_item_id": {"type": "integer"},
        "percentage": {"type": "number"},
        "start_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "end_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "category": {"type": "string", "enum": ["researcher", "undergrad", "student", "professor"]},
        "status": {"type": "string", "enum": ["planned", "contract", "rebook"]},
        "salary_category": {"type": "string", "description": "Name der Entgeltgruppe, z. B. E13"},
        "start_level": {"type": "integer", "minimum": 1, "maximum": 6},
        "level_start_date": {"type": "string", "description": "JJJJ-MM-TT, optional"},
        "statutory_health_insurance": {"type": "boolean"},
    },
    required=["budget_item_id", "percentage", "start_date", "end_date"],
    write=True,
)
def plan_employment(user, budget_item_id, percentage, start_date, end_date, staff_id=None, first_name="",
                    last_name="", category="researcher", status="planned", salary_category=None, start_level=None,
                    level_start_date=None, statutory_health_insurance=True):
    from staffing.forms import PlanEmploymentForm
    from staffing.models import SalaryCategory
    from staffing.utils import create_planned_employment

    category_id = None
    if salary_category:
        found = SalaryCategory.objects.filter(name__iexact=salary_category).first()
        if found is None:
            raise ToolError(f"Entgeltgruppe „{salary_category}“ nicht gefunden.")
        category_id = found.id
    data = {
        "staff_member": staff_id or "",
        "first_name": first_name,
        "last_name": last_name,
        "budget_item": budget_item_id,
        "percentage": percentage,
        "start_date": start_date,
        "end_date": end_date,
        "category": category,
        "status": status,
        "salary_category": category_id or "",
        "start_level": start_level or "",
        "level_start_date": level_start_date or "",
    }
    if statutory_health_insurance:
        data["statutory_health_insurance"] = "on"
    form = PlanEmploymentForm(data)
    if not form.is_valid():
        raise ToolError("; ".join(f"{field}: {' '.join(errors)}" for field, errors in form.errors.items()))
    employment, missing = create_planned_employment(form.cleaned_data)
    return {
        "employment_id": employment.id,
        "staff_id": employment.staff_member_id,
        "person": str(employment.staff_member),
        "salaries_estimated": missing is not None,
        "months_without_table_amount": missing or [],
    }


@tool(
    "ignore_warning",
    "Ignoriert eine Warnung mit Begründung (Schlüssel aus list_warnings). Sie erscheint wieder, wenn sie sich ändert.",
    {"key": {"type": "string"}, "comment": {"type": "string"}},
    required=["key", "comment"],
    write=True,
)
def ignore_warning(user, key, comment):
    from controlling.models import IgnoredWarning

    comment = (comment or "").strip()
    if not comment:
        raise ToolError("Bitte eine Begründung angeben.")
    if len(key or "") != 64:
        raise ToolError("Ungültiger Schlüssel.")
    IgnoredWarning.objects.update_or_create(key=key, defaults={"comment": comment, "created_by": user})
    return {"ignored": True}
