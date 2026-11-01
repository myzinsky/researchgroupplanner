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


def _allocation_data(allocation):
    return {
        "allocation_id": allocation.id,
        "source": _allocation_source(allocation),
        "budget_item_id": allocation.budget_item_id,
        "percentage": _money(allocation.percentage),
        "start": _day(allocation.start_date),
        "end": _day(allocation.end_date or allocation.employment.end_date),
        "sap_reference": allocation.sap_reference,
        "open_rebookings": [_rebooking_data(r) for r in allocation.rebookings.select_related("budget_item__project")],
    }


def _budget_item_data(item, kind):
    data = {"budget_item_id": item.id, "kind": kind, "project": item.project.acronym, "amount": _money(item.amount)}
    if kind != "overhead":
        data["title"] = item.title
    if kind == "staff":
        data["allowed_categories"] = sorted(e.eligible_employment for e in item.staffbudgetitemeligibility_set.all())
    return data


def _employment_data(employment):
    """An employment as listed by get_staff_member: allocations and planned vs. SAP monthly costs."""
    from staffing.utils import get_salaries_by_month, get_sap_actuals_by_month

    allocations = list(employment.stafffundingallocation_set.select_related(
        "budget_item__project", "landesstelle", "annual_pool_budget__annual_pool",
    ).order_by("start_date"))
    planned = get_salaries_by_month(employment)
    actuals = get_sap_actuals_by_month(allocations)
    return {
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
        "has_bachelor": employment.has_bachelor,
        "allocations": [_allocation_data(a) for a in allocations],
        "monthly_costs": {
            month: {
                "planned": _money(amount),
                "sap_actual": _money(sum((e["amount"] for e in actuals[month]), Decimal("0"))) if month in actuals else None,
            }
            for month, amount in planned.items()
        },
    }


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
    "Budgetposten: Personal (IDs für Umbuchungen und Anstellungsplanung, mit erlaubten Beschäftigtenkategorien), "
    "Sachmittel und Overhead, optional nur eines Projekts.",
    {"acronym": {"type": "string"}},
)
def list_budget_items(user, acronym=None):
    from projects.models import OtherBudgetItem, OverheadBudgetItem, StaffBudgetItem

    def scoped(model):
        items = model.objects.select_related("project").order_by("project__acronym", "id")
        return items.filter(project=_project(acronym)) if acronym else items

    return {
        "staff_budget_items": [
            _budget_item_data(i, "staff")
            for i in scoped(StaffBudgetItem).prefetch_related("staffbudgetitemeligibility_set").order_by("project__acronym", "title")
        ],
        "other_budget_items": [_budget_item_data(i, "other") for i in scoped(OtherBudgetItem).order_by("project__acronym", "title")],
        "overhead_budget_items": [_budget_item_data(i, "overhead") for i in scoped(OverheadBudgetItem)],
    }


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
    from staffing.utils import staff_reservations

    member = _staff_member(staff_id, name)
    employments = [_employment_data(employment) for employment in member.employment_set.order_by("start_date")]
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
    "Gehälter aus den TV-L-Tabellen geschätzt, für category=student ohne Entgeltgruppe aus dem SHK-Stundensatz "
    "(has_bachelor: Satz mit Bachelor; 40 h/Woche = 100 %).",
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
        "has_bachelor": {"type": "boolean"},
    },
    required=["budget_item_id", "percentage", "start_date", "end_date"],
    write=True,
)
def plan_employment(user, budget_item_id, percentage, start_date, end_date, staff_id=None, first_name="",
                    last_name="", category="researcher", status="planned", salary_category=None, start_level=None,
                    level_start_date=None, statutory_health_insurance=True, has_bachelor=False):
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
    if has_bachelor:
        data["has_bachelor"] = "on"
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


EMPLOYMENT_FIELDS = (
    "start_date", "end_date", "percentage", "status", "category", "salary_category", "start_level",
    "level_start_date", "statutory_health_insurance", "has_bachelor",
)
# Changes that alter the monthly costs and therefore trigger a new salary estimate.
COST_FIELDS = {
    "start_date", "end_date", "percentage", "salary_category", "start_level", "level_start_date",
    "statutory_health_insurance", "category", "has_bachelor",
}


def _employment(employment_id):
    from staffing.models import Employment

    employment = Employment.objects.select_related("staff_member", "salary_category").filter(id=employment_id).first()
    if employment is None:
        raise ToolError(f"Keine Anstellung mit ID {employment_id}.")
    return employment


def _allocation_label(allocation):
    end = allocation.end_date or allocation.employment.end_date
    return f"{allocation.id}: {_allocation_source(allocation)} ({_day(allocation.start_date)} – {_day(end)})"


def _cost_snapshot(employment, budget_item_ids=()):
    """{(budget item id, source): planned cost}: the totals (all people) of the staff budget items the employment
    is allocated to plus budget_item_ids, and the employment's own costs on other funding sources."""
    from projects.models import StaffBudgetItem
    from projects.utils import calculate_salary_for_allocation, get_staff_budget_item_used

    costs = {}
    item_ids = set(budget_item_ids)
    allocations = employment.stafffundingallocation_set.select_related(
        "employment", "landesstelle", "annual_pool_budget__annual_pool",
    ) if employment is not None and employment.pk else []
    for allocation in allocations:
        if allocation.budget_item_id:
            item_ids.add(allocation.budget_item_id)
            continue
        key = (None, _allocation_source(allocation))
        costs[key] = costs.get(key, Decimal("0.00")) + calculate_salary_for_allocation(allocation).salary_sum
    for item in StaffBudgetItem.objects.filter(id__in=item_ids).select_related("project"):
        costs[(item.id, f"{item.project.acronym} – {item.title}")] = get_staff_budget_item_used(item)
    return costs


def _item_ids(costs):
    return {budget_item_id for budget_item_id, _ in costs if budget_item_id}


def _cost_changes(before, after):
    return [
        {
            "budget_item_id": budget_item_id,
            "source": source,
            "before": _money(before.get((budget_item_id, source), Decimal("0.00"))),
            "after": _money(after.get((budget_item_id, source), Decimal("0.00"))),
            "change": _money(after.get((budget_item_id, source), Decimal("0.00"))
                             - before.get((budget_item_id, source), Decimal("0.00"))),
        }
        for budget_item_id, source in sorted(set(before) | set(after), key=lambda key: key[1])
    ]


def _form_errors(form):
    return "; ".join(
        f"{field}: {' '.join(errors)}" if field != "__all__" else " ".join(errors)
        for field, errors in form.errors.items()
    )


@tool(
    "update_employment",
    "Ändert eine Anstellung (employment_id aus get_staff_member); nur übergebene Felder werden geändert, null leert "
    "ein Feld. Bei Änderungen an Zeitraum, Umfang, Entgeltgruppe, Stufe oder Versicherung werden die geplanten "
    "Gehälter wie bei plan_employment neu geschätzt (reestimate=true erzwingt das), Monate mit SAP-Ist bleiben "
    "unverändert (dafür apply_sap_salaries). Zuordnungen außerhalb "
    "des neuen Zeitraums sind ein Fehler, mit clip_allocations=true werden sie gekürzt bzw. gelöscht. "
    "dry_run=true prüft nur und zeigt das Ergebnis samt Kostenänderung je Budget, ohne zu speichern.",
    {
        "employment_id": {"type": "integer"},
        "start_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "end_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "percentage": {"type": "number"},
        "status": {"type": "string", "enum": ["planned", "contract", "rebook"]},
        "category": {"type": "string", "enum": ["researcher", "undergrad", "student", "professor"]},
        "salary_category": {"type": ["string", "null"], "description": "Name der Entgeltgruppe, z. B. E13"},
        "start_level": {"type": ["integer", "null"], "minimum": 1, "maximum": 6},
        "level_start_date": {"type": ["string", "null"], "description": "JJJJ-MM-TT"},
        "statutory_health_insurance": {"type": "boolean"},
        "has_bachelor": {"type": "boolean", "description": "SHK mit Bachelor (Stundensatz)"},
        "clip_allocations": {"type": "boolean"},
        "reestimate": {"type": "boolean", "description": "Gehälter auch ohne Feldänderung neu schätzen"},
        "dry_run": {"type": "boolean"},
    },
    required=["employment_id"],
    write=True,
)
def update_employment(user, employment_id, clip_allocations=False, reestimate=False, dry_run=False, **changes):
    from django.db import transaction
    from django.forms.models import model_to_dict

    from staffing.forms import EmploymentForm
    from staffing.models import Rebooking, SalaryCategory
    from staffing.utils import apply_tariff_salaries, estimate_from_month, get_sap_actuals_by_month

    unknown = sorted(set(changes) - set(EMPLOYMENT_FIELDS))
    if unknown:
        raise ToolError("Unbekannte Felder: " + ", ".join(unknown))
    if not changes and not reestimate:
        raise ToolError("Keine Änderungen angegeben.")
    employment = _employment(employment_id)

    data = {key: "" if value is None else value for key, value in model_to_dict(employment, EMPLOYMENT_FIELDS).items()}
    for field, value in changes.items():
        if field == "salary_category" and value not in (None, ""):
            found = SalaryCategory.objects.filter(name__iexact=value).first()
            if found is None:
                raise ToolError(f"Entgeltgruppe „{value}“ nicht gefunden.")
            value = found.id
        data[field] = "" if value is None else value
    for flag in ("statutory_health_insurance", "has_bachelor"):
        if data.pop(flag):
            data[flag] = "on"
    form = EmploymentForm(data, instance=employment)
    if not form.is_valid():
        raise ToolError(_form_errors(form))
    new_start, new_end = form.cleaned_data["start_date"], form.cleaned_data["end_date"]

    allocations = list(employment.stafffundingallocation_set.select_related(
        "employment", "budget_item__project", "landesstelle", "annual_pool_budget__annual_pool",
    ).order_by("start_date"))
    outside = [
        a for a in allocations
        if a.start_date < new_start or a.start_date > new_end or (a.end_date and a.end_date > new_end)
    ]
    if outside and not clip_allocations:
        raise ToolError(
            f"Zuordnungen liegen außerhalb des neuen Zeitraums ({_day(new_start)} – {_day(new_end)}): "
            + "; ".join(_allocation_label(a) for a in outside)
            + ". Mit clip_allocations=true werden sie gekürzt bzw. gelöscht."
        )

    before = _cost_snapshot(employment)
    sap_actuals = get_sap_actuals_by_month(allocations)
    cost_relevant = reestimate or bool(COST_FIELDS & set(form.changed_data))
    clipped, deleted = [], []
    with transaction.atomic():
        employment = form.save()
        for allocation in outside:
            label = _allocation_label(allocation)
            allocation.employment = employment
            allocation.start_date = max(allocation.start_date, new_start)
            if allocation.end_date and allocation.end_date > new_end:
                allocation.end_date = new_end
            if allocation.start_date > (allocation.end_date or new_end):
                allocation.delete()
                deleted.append(label)
                continue
            try:
                allocation.full_clean()
            except ValidationError as error:
                raise ToolError(f"Zuordnung {label}: " + " ".join(error.messages))
            allocation.save()
            clipped.append(_allocation_label(allocation))
        for rebooking in Rebooking.objects.filter(allocation__employment=employment).select_related("allocation__employment"):
            try:
                rebooking.full_clean()
            except ValidationError as error:
                raise ToolError(f"Offene Umbuchung {rebooking.id} passt nicht mehr: " + " ".join(error.messages))

        estimated, missing = False, []
        if cost_relevant and employment.has_salary_basis():
            missing = apply_tariff_salaries(employment, from_month=estimate_from_month(employment, sap_actuals))
            estimated = True
        employment.refresh_from_db()
        result = {
            "dry_run": bool(dry_run),
            "employment": _employment_data(employment),
            "salaries_estimated": estimated,
            "months_without_table_amount": missing,
            "salaries_kept_until": max(sap_actuals) if estimated and sap_actuals else None,
            "clipped_allocations": clipped,
            "deleted_allocations": deleted,
            "cost_changes": _cost_changes(before, _cost_snapshot(employment, _item_ids(before))),
        }
        if cost_relevant and not estimated:
            result["notice"] = "Ohne Entgeltgruppe und Stufe (oder SHK-Stundensatz) wurden die Gehälter nicht neu geschätzt."
        if dry_run:
            transaction.set_rollback(True)
    return result


@tool(
    "delete_employment",
    "Löscht eine Anstellung mit ihren geplanten Gehältern. Mit Zuordnungen nur bei delete_allocations=true; "
    "nie bei offenen Umbuchungen oder SAP-Ist-Gehältern. dry_run=true zeigt nur, was gelöscht würde.",
    {
        "employment_id": {"type": "integer"},
        "delete_allocations": {"type": "boolean"},
        "dry_run": {"type": "boolean"},
    },
    required=["employment_id"],
    write=True,
)
def delete_employment(user, employment_id, delete_allocations=False, dry_run=False):
    from staffing.models import Rebooking
    from staffing.utils import get_sap_actuals_by_month

    employment = _employment(employment_id)
    allocations = list(employment.stafffundingallocation_set.select_related(
        "employment", "budget_item__project", "landesstelle", "annual_pool_budget__annual_pool",
    ).order_by("start_date"))
    rebookings = list(Rebooking.objects.filter(allocation__employment=employment).values_list("id", flat=True))
    if rebookings:
        raise ToolError("Die Anstellung hat offene Umbuchungen (IDs " + ", ".join(map(str, rebookings))
                        + "); bitte zuerst abschließen oder löschen.")
    sap_actuals = get_sap_actuals_by_month(allocations)
    if sap_actuals:
        raise ToolError("Zur Anstellung gibt es SAP-Ist-Gehälter (" + ", ".join(sorted(sap_actuals))
                        + "); sie wird nicht gelöscht.")
    if allocations and not delete_allocations:
        raise ToolError("Die Anstellung hat Zuordnungen: " + "; ".join(_allocation_label(a) for a in allocations)
                        + ". Mit delete_allocations=true werden sie mitgelöscht.")
    from django.db import transaction

    before = _cost_snapshot(employment)
    with transaction.atomic():
        result = {
            "dry_run": bool(dry_run),
            "deleted": not dry_run,
            "employment": _employment_data(employment),
            "deleted_allocations": [_allocation_label(a) for a in allocations],
        }
        employment.delete()
        result["cost_changes"] = _cost_changes(before, _cost_snapshot(None, _item_ids(before)))
        if dry_run:
            transaction.set_rollback(True)
    return result


ALLOCATION_FIELDS = ("budget_item_id", "percentage", "start_date", "end_date", "sap_reference")


def _allocation(allocation_id):
    from staffing.models import StaffFundingAllocation

    allocation = StaffFundingAllocation.objects.select_related(
        "employment__staff_member", "budget_item__project", "landesstelle", "annual_pool_budget__annual_pool",
    ).filter(id=allocation_id).first()
    if allocation is None:
        raise ToolError(f"Keine Zuordnung mit ID {allocation_id}.")
    return allocation


def _refuse_open_rebookings(allocation):
    ids = list(allocation.rebookings.values_list("id", flat=True))
    if ids:
        raise ToolError("Die Zuordnung hat offene Umbuchungen (IDs " + ", ".join(map(str, ids))
                        + "); bitte zuerst abschließen oder löschen.")


def _save_allocation(allocation, changes, dry_run):
    """Validate (AllocationForm, model rules, allocation_conflicts) and save; returns the tool result."""
    from django.db import transaction
    from django.forms.models import model_to_dict

    from projects.models import StaffBudgetItem
    from staffing.forms import AllocationForm
    from staffing.utils import allocation_conflicts

    if "budget_item_id" in changes:
        budget_item = StaffBudgetItem.objects.select_related("project").filter(id=changes["budget_item_id"]).first()
        if budget_item is None:
            raise ToolError(f"Kein Personalbudget mit ID {changes['budget_item_id']}.")
        # Moving to a project budget replaces any other funding source.
        allocation.budget_item = budget_item
        allocation.landesstelle = allocation.annual_pool_budget = None
        allocation.is_universal = False
    data = {key: "" if value is None else value
            for key, value in model_to_dict(allocation, ["budget_item", "percentage", "start_date", "end_date"]).items()}
    for field in ("percentage", "start_date", "end_date"):
        if field in changes:
            data[field] = "" if changes[field] is None else changes[field]
    form = AllocationForm(data, instance=allocation)
    if not form.is_valid():
        raise ToolError(_form_errors(form))
    if "sap_reference" in changes:
        allocation.sap_reference = (changes["sap_reference"] or "").strip()
        if len(allocation.sap_reference) > 50:
            raise ToolError("sap_reference: höchstens 50 Zeichen.")
    problems = allocation_conflicts(allocation)
    if problems:
        raise ToolError(" ".join(problems))

    employment = allocation.employment
    before = _cost_snapshot(employment, [allocation.budget_item_id] if allocation.budget_item_id else [])
    with transaction.atomic():
        allocation.save()
        result = {
            "dry_run": bool(dry_run),
            "allocation": _allocation_data(allocation),
            "cost_changes": _cost_changes(before, _cost_snapshot(employment, _item_ids(before))),
        }
        if dry_run:
            transaction.set_rollback(True)
    return result


@tool(
    "create_allocation",
    "Ordnet eine Anstellung (employment_id) einem Personalbudget (budget_item_id aus list_budget_items) zu. Die "
    "Zuordnung muss in Anstellung und Projektlaufzeit liegen, die Summe der Zuordnungen darf den Umfang der "
    "Anstellung nicht übersteigen. dry_run=true prüft nur und zeigt die Kostenänderung je Budget.",
    {
        "employment_id": {"type": "integer"},
        "budget_item_id": {"type": "integer"},
        "percentage": {"type": "number"},
        "start_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "end_date": {"type": ["string", "null"], "description": "JJJJ-MM-TT, null = Ende der Anstellung"},
        "sap_reference": {"type": "string", "description": "Referenzbelegnummer der SAP-Mittelreservierung"},
        "dry_run": {"type": "boolean"},
    },
    required=["employment_id", "budget_item_id", "percentage", "start_date", "end_date"],
    write=True,
)
def create_allocation(user, employment_id, budget_item_id, percentage, start_date, end_date, sap_reference="",
                      dry_run=False):
    from staffing.models import StaffFundingAllocation

    allocation = StaffFundingAllocation(employment=_employment(employment_id))
    return _save_allocation(allocation, {
        "budget_item_id": budget_item_id, "percentage": percentage, "start_date": start_date, "end_date": end_date,
        "sap_reference": sap_reference,
    }, dry_run)


@tool(
    "update_allocation",
    "Ändert eine Zuordnung (allocation_id aus get_staff_member); nur übergebene Felder werden geändert. "
    "budget_item_id verschiebt sie auf ein anderes Personalbudget, end_date=null bedeutet Ende der Anstellung. "
    "Nicht bei offenen Umbuchungen. dry_run=true prüft nur und zeigt die Kostenänderung je Budget.",
    {
        "allocation_id": {"type": "integer"},
        "budget_item_id": {"type": "integer"},
        "percentage": {"type": "number"},
        "start_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "end_date": {"type": ["string", "null"], "description": "JJJJ-MM-TT"},
        "sap_reference": {"type": "string"},
        "dry_run": {"type": "boolean"},
    },
    required=["allocation_id"],
    write=True,
)
def update_allocation(user, allocation_id, dry_run=False, **changes):
    unknown = sorted(set(changes) - set(ALLOCATION_FIELDS))
    if unknown:
        raise ToolError("Unbekannte Felder: " + ", ".join(unknown))
    if not changes:
        raise ToolError("Keine Änderungen angegeben.")
    allocation = _allocation(allocation_id)
    _refuse_open_rebookings(allocation)
    return _save_allocation(allocation, changes, dry_run)


@tool(
    "delete_allocation",
    "Löscht eine Zuordnung (nicht bei offenen Umbuchungen). dry_run=true zeigt nur die Kostenänderung je Budget.",
    {"allocation_id": {"type": "integer"}, "dry_run": {"type": "boolean"}},
    required=["allocation_id"],
    write=True,
)
def delete_allocation(user, allocation_id, dry_run=False):
    from django.db import transaction

    allocation = _allocation(allocation_id)
    _refuse_open_rebookings(allocation)
    employment = allocation.employment
    before = _cost_snapshot(employment)
    with transaction.atomic():
        result = {"dry_run": bool(dry_run), "deleted": not dry_run, "allocation": _allocation_data(allocation)}
        allocation.delete()
        result["cost_changes"] = _cost_changes(before, _cost_snapshot(employment, _item_ids(before)))
        if dry_run:
            transaction.set_rollback(True)
    return result


@tool(
    "update_staff_member",
    "Ändert die Stammdaten einer Person wie im Admin; nur übergebene Felder werden geändert. status: in_hire "
    "(Einstellung), active (aktiv) oder alumni (ehemalig). dry_run=true prüft nur.",
    {
        "staff_id": {"type": "integer"},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "email": {"type": "string"},
        "sap_business_partner": {"type": "string"},
        "is_leadership": {"type": "boolean"},
        "status": {"type": "string", "enum": ["in_hire", "active", "alumni"]},
        "dry_run": {"type": "boolean"},
    },
    required=["staff_id"],
    write=True,
)
def update_staff_member(user, staff_id, dry_run=False, **changes):
    from django.forms.models import model_to_dict

    from staffing.forms import StaffMemberForm

    fields = StaffMemberForm._meta.fields
    unknown = sorted(set(changes) - set(fields))
    if unknown:
        raise ToolError("Unbekannte Felder: " + ", ".join(unknown))
    if not changes:
        raise ToolError("Keine Änderungen angegeben.")
    member = _staff_member(staff_id)
    data = {**model_to_dict(member, fields), **{k: "" if v is None else v for k, v in changes.items()}}
    if data.pop("is_leadership"):
        data["is_leadership"] = "on"
    form = StaffMemberForm(data, instance=member)
    if not form.is_valid():
        raise ToolError(_form_errors(form))
    if not dry_run:
        form.save()
    return {
        "dry_run": bool(dry_run),
        "staff_id": member.id,
        "changed": form.changed_data,
        **{field: getattr(member, field) for field in fields},
        "status_display": member.get_status_display(),
    }


@tool(
    "merge_employments",
    "Führt direkt aneinander anschließende Anstellungen einer Person zu einer zusammen (gleiche Regeln wie der "
    "Hinweis „kann zusammengeführt werden“). Zuordnungen und Gehälter bleiben erhalten. dry_run=true prüft nur.",
    {
        "employment_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 2},
        "dry_run": {"type": "boolean"},
    },
    required=["employment_ids"],
    write=True,
)
def merge_employments(user, employment_ids, dry_run=False):
    from django.db import transaction

    from staffing.utils import merge_employments as merge

    if not isinstance(employment_ids, list) or len(set(employment_ids)) < 2:
        raise ToolError("Bitte mindestens zwei verschiedene employment_ids angeben.")
    employments = sorted((_employment(i) for i in set(employment_ids)), key=lambda e: e.start_date)
    before = _cost_snapshot(employments[0])
    for other in employments[1:]:
        before.update(_cost_snapshot(other))
    with transaction.atomic():
        merged = employments[0]
        for following in employments[1:]:
            try:
                merged = merge(merged, following)
            except ValueError as error:
                raise ToolError(f"Anstellungen {merged.id} und {following.id}: {error}")
        merged.refresh_from_db()
        result = {
            "dry_run": bool(dry_run),
            "employment": _employment_data(merged),
            "cost_changes": _cost_changes(before, _cost_snapshot(merged, _item_ids(before))),
        }
        if dry_run:
            transaction.set_rollback(True)
    return result


BUDGET_KINDS = ("staff", "other", "overhead")


def _budget_item_model(kind):
    from projects.models import OtherBudgetItem, OverheadBudgetItem, StaffBudgetItem

    if kind not in BUDGET_KINDS:
        raise ToolError(f"kind muss eines von {', '.join(BUDGET_KINDS)} sein.")
    return {"staff": StaffBudgetItem, "other": OtherBudgetItem, "overhead": OverheadBudgetItem}[kind]


def _save_budget_item(item, kind, data, allowed_categories, dry_run):
    """Validate with a ModelForm of the item's fields, set the allowed categories and save (or roll back)."""
    from django.db import transaction
    from django.forms import modelform_factory

    from projects.models import EmploymentCategories, StaffBudgetItemEligibility

    fields = ["amount"] if kind == "overhead" else ["title", "amount"]
    form = modelform_factory(type(item), fields=fields)(data, instance=item)
    if not form.is_valid():
        raise ToolError(_form_errors(form))
    if allowed_categories is not None:
        if kind != "staff":
            raise ToolError("allowed_categories gibt es nur bei Personalbudgets.")
        unknown = sorted(set(allowed_categories) - set(EmploymentCategories))
        if unknown:
            raise ToolError(f"Unbekannte Kategorien: {', '.join(unknown)} (erlaubt: {', '.join(EmploymentCategories)}).")
    created = item.pk is None
    with transaction.atomic():
        form.save()
        if allowed_categories is not None:
            item.staffbudgetitemeligibility_set.exclude(eligible_employment__in=allowed_categories).delete()
            existing = set(item.staffbudgetitemeligibility_set.values_list("eligible_employment", flat=True))
            StaffBudgetItemEligibility.objects.bulk_create(
                StaffBudgetItemEligibility(budget_item=item, eligible_employment=category)
                for category in sorted(set(allowed_categories) - existing)
            )
        result = {"dry_run": bool(dry_run), "created": created, "budget_item": _budget_item_data(item, kind)}
        if kind == "staff":
            from projects.utils import get_staff_budget_item_used

            result["planned_staff_costs"] = _money(get_staff_budget_item_used(item))
        if dry_run:
            transaction.set_rollback(True)
            if created:
                result["budget_item"]["budget_item_id"] = None
    return result


@tool(
    "create_budget_item",
    "Legt einen Budgetposten eines Projekts an: kind staff (Personal), other (Sachmittel) oder overhead (ohne "
    "Titel). allowed_categories nur für Personal (researcher, undergrad, student, professor). dry_run=true prüft nur.",
    {
        "project": {"type": "string", "description": "Akronym"},
        "kind": {"type": "string", "enum": list(BUDGET_KINDS)},
        "title": {"type": "string"},
        "amount": {"type": "number"},
        "allowed_categories": {"type": "array", "items": {"type": "string"}},
        "dry_run": {"type": "boolean"},
    },
    required=["project", "kind", "amount"],
    write=True,
)
def create_budget_item(user, project, kind, amount, title="", allowed_categories=None, dry_run=False):
    model = _budget_item_model(kind)
    if kind != "overhead" and not (title or "").strip():
        raise ToolError("title: Bitte einen Titel angeben.")
    item = model(project=_project(project))
    return _save_budget_item(item, kind, {"title": title, "amount": amount}, allowed_categories, dry_run)


@tool(
    "update_budget_item",
    "Ändert einen Budgetposten (IDs aus list_budget_items, kind wie dort, Standard staff); nur übergebene Felder "
    "werden geändert. allowed_categories ersetzt die erlaubten Beschäftigtenkategorien eines Personalbudgets. "
    "dry_run=true prüft nur.",
    {
        "budget_item_id": {"type": "integer"},
        "kind": {"type": "string", "enum": list(BUDGET_KINDS)},
        "title": {"type": "string"},
        "amount": {"type": "number"},
        "allowed_categories": {"type": "array", "items": {"type": "string"}},
        "dry_run": {"type": "boolean"},
    },
    required=["budget_item_id"],
    write=True,
)
def update_budget_item(user, budget_item_id, kind="staff", title=None, amount=None, allowed_categories=None,
                       dry_run=False):
    model = _budget_item_model(kind)
    item = model.objects.select_related("project").filter(id=budget_item_id).first()
    if item is None:
        raise ToolError(f"Kein Budgetposten ({kind}) mit ID {budget_item_id}.")
    if title is None and amount is None and allowed_categories is None:
        raise ToolError("Keine Änderungen angegeben.")
    if title is not None and kind == "overhead":
        raise ToolError("Overhead-Posten haben keinen Titel.")
    data = {
        "title": getattr(item, "title", "") if title is None else title,
        "amount": item.amount if amount is None else amount,
    }
    result = _save_budget_item(item, kind, data, allowed_categories, dry_run)
    if dry_run:
        item.refresh_from_db()
    return result


@tool(
    "delete_staff_member",
    "Löscht eine Person. Nur ohne Anstellungen, Zuordnungen und SAP-Daten (Namenszuordnungen, "
    "Mittelreservierungen). dry_run=true prüft nur.",
    {"staff_id": {"type": "integer"}, "dry_run": {"type": "boolean"}},
    required=["staff_id"],
    write=True,
)
def delete_staff_member(user, staff_id, dry_run=False):
    from staffing.models import StaffFundingAllocation
    from staffing.utils import staff_reservations

    member = _staff_member(staff_id)
    problems = []
    employments = list(member.employment_set.values_list("id", flat=True))
    if employments:
        problems.append("Anstellungen (IDs " + ", ".join(map(str, employments)) + ")")
    allocations = list(StaffFundingAllocation.objects.filter(employment__staff_member=member).values_list("id", flat=True))
    if allocations:
        problems.append("Zuordnungen (IDs " + ", ".join(map(str, allocations)) + ")")
    mappings = list(member.sap_name_mappings.values_list("sap_name", flat=True))
    if mappings:
        problems.append("SAP-Namenszuordnungen (" + ", ".join(mappings) + ")")
    reservations = staff_reservations(member)
    if reservations:
        problems.append("SAP-Mittelreservierungen (" + ", ".join(
            f"{r['project'].acronym} {r['position'].reference}" for r in reservations
        ) + ")")
    if problems:
        raise ToolError(f"{member} wird nicht gelöscht, es gibt noch: " + "; ".join(problems) + ".")
    result = {"dry_run": bool(dry_run), "deleted": not dry_run, "staff_id": member.id, "name": str(member)}
    if not dry_run:
        member.delete()
    return result


@tool(
    "set_monthly_salaries",
    "Setzt geplante Monatsgehälter (Kosten inkl. Arbeitgeberanteil) einer Anstellung manuell, z. B. "
    "{\"2027-01\": 1234.56}. Andere Monate bleiben unverändert; Monate mit SAP-Ist werden nicht überschrieben. "
    "Teilmonate gelten für die Tage der Anstellung. dry_run=true prüft nur.",
    {
        "employment_id": {"type": "integer"},
        "salaries": {"type": "object", "additionalProperties": {"type": "number"},
                     "description": "{JJJJ-MM: Betrag}"},
        "dry_run": {"type": "boolean"},
    },
    required=["employment_id", "salaries"],
    write=True,
)
def set_monthly_salaries(user, employment_id, salaries, dry_run=False):
    import re
    from decimal import InvalidOperation

    from django.db import transaction

    from staffing.utils import get_sap_actuals_by_month, set_monthly_salaries as store

    if not isinstance(salaries, dict) or not salaries:
        raise ToolError("salaries: Bitte {JJJJ-MM: Betrag} angeben.")
    amounts = {}
    for month, value in salaries.items():
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month)):
            raise ToolError(f"Monat im Format JJJJ-MM erwartet, erhalten: {month!r}")
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise ToolError(f"{month}: Betrag erwartet, erhalten: {value!r}")
        if amount < 0:
            raise ToolError(f"{month}: Der Betrag darf nicht negativ sein.")
        amounts[month] = amount
    employment = _employment(employment_id)
    sap_months = sorted(set(amounts) & set(get_sap_actuals_by_month(employment.stafffundingallocation_set.all())))
    if sap_months:
        raise ToolError("Für diese Monate gibt es SAP-Ist, sie werden nicht überschrieben: " + ", ".join(sap_months))

    before = _cost_snapshot(employment)
    with transaction.atomic():
        try:
            store(employment, amounts)
        except ValueError as error:
            raise ToolError(str(error))
        result = {
            "dry_run": bool(dry_run),
            "employment": _employment_data(employment),
            "cost_changes": _cost_changes(before, _cost_snapshot(employment, _item_ids(before))),
        }
        if dry_run:
            transaction.set_rollback(True)
    return result


@tool(
    "apply_sap_salaries",
    "Übernimmt die SAP-Ist-Gehälter der verknüpften SAP-Positionen (sap_reference der Zuordnungen) als geplante "
    "Monatsgehälter der Anstellung, wie „Gehälter übernehmen“ auf der SAP-Positionsseite. Andere Monate bleiben "
    "unverändert. dry_run=true prüft nur.",
    {
        "employment_id": {"type": "integer"},
        "dry_run": {"type": "boolean"},
    },
    required=["employment_id"],
    write=True,
)
def apply_sap_salaries(user, employment_id, dry_run=False):
    from django.db import transaction

    from sap_integration.crosscheck import references
    from sap_integration.models import SAPPosition
    from sap_integration.transform import apply_monthly_salaries

    employment = _employment(employment_id)
    allocations = [a for a in employment.stafffundingallocation_set.all() if references(a.sap_reference)]
    if not allocations:
        raise ToolError("Keine Zuordnung der Anstellung ist mit einer SAP-Position verknüpft.")
    refs = {ref for a in allocations for ref in references(a.sap_reference)}
    latest, positions = {}, []
    for position in SAPPosition.objects.filter(reference__in=refs).select_related("sap_import__fund").order_by(
            "-sap_import__imported_at"):
        fund_id = position.sap_import.fund_id
        latest.setdefault(fund_id, position.sap_import_id)
        if latest[fund_id] == position.sap_import_id and position.monthly_actuals:
            positions.append(position)
    if not positions:
        raise ToolError("Zu den verknüpften SAP-Positionen gibt es keine Ist-Gehälter.")

    before = _cost_snapshot(employment)
    applied = set()
    with transaction.atomic():
        for position in positions:
            costs = {month: Decimal(amount) for month, amount in position.monthly_actuals.items()}
            for allocation in allocations:
                if position.reference in references(allocation.sap_reference):
                    allocation.refresh_from_db()
                    skipped = set(apply_monthly_salaries(allocation, costs))
                    applied |= {m for m, c in costs.items() if c > 0} - skipped
        employment.refresh_from_db()
        result = {
            "dry_run": bool(dry_run),
            "months_applied": sorted(applied),
            "employment": _employment_data(employment),
            "cost_changes": _cost_changes(before, _cost_snapshot(employment, _item_ids(before))),
        }
        if dry_run:
            transaction.set_rollback(True)
    return result


PROJECT_FIELDS = {
    "start_date": "start_date", "end_date": "end_date", "extension_until": "extension_planning_date",
    "budget_total": "budget_total", "no_overhead": "no_overhead",
}


@tool(
    "update_project",
    "Ändert Stammdaten eines Projekts: Laufzeit (start_date, end_date), kostenneutrale Verlängerung "
    "(extension_until, null entfernt sie), Fördersumme (budget_total) und no_overhead (z. B. Auftragsforschung). "
    "Nur übergebene Felder werden geändert. dry_run=true prüft nur.",
    {
        "acronym": {"type": "string"},
        "start_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "end_date": {"type": "string", "description": "JJJJ-MM-TT"},
        "extension_until": {"type": ["string", "null"], "description": "JJJJ-MM-TT"},
        "budget_total": {"type": "number"},
        "no_overhead": {"type": "boolean"},
        "dry_run": {"type": "boolean"},
    },
    required=["acronym"],
    write=True,
)
def update_project(user, acronym, dry_run=False, **changes):
    from datetime import date
    from decimal import InvalidOperation

    from django.db import transaction

    unknown = sorted(set(changes) - set(PROJECT_FIELDS))
    if unknown:
        raise ToolError("Unbekannte Felder: " + ", ".join(unknown))
    if not changes:
        raise ToolError("Keine Änderungen angegeben.")
    project = _project(acronym)
    for field, value in changes.items():
        attribute = PROJECT_FIELDS[field]
        if field in ("start_date", "end_date", "extension_until"):
            if value is None and field != "extension_until":
                raise ToolError(f"{field} darf nicht leer sein.")
            try:
                value = None if value is None else date.fromisoformat(value)
            except (TypeError, ValueError):
                raise ToolError(f"{field}: Datum im Format JJJJ-MM-TT erwartet, erhalten: {value!r}")
        elif field == "budget_total":
            try:
                value = Decimal(str(value)).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                raise ToolError(f"budget_total: Betrag erwartet, erhalten: {value!r}")
        elif field == "no_overhead":
            value = bool(value)
        setattr(project, attribute, value)
    if project.end_date < project.start_date:
        raise ToolError("Das Projektende darf nicht vor dem Projektbeginn liegen.")
    try:
        project.full_clean()
    except ValidationError as error:
        raise ToolError(" ".join(error.messages))
    with transaction.atomic():
        project.save()
        result = {
            "dry_run": bool(dry_run),
            "acronym": project.acronym,
            "start": _day(project.start_date),
            "end": _day(project.end_date),
            "extension_until": _day(project.extension_planning_date),
            "budget_total": _money(project.budget_total),
            "no_overhead": project.no_overhead,
        }
        if dry_run:
            transaction.set_rollback(True)
    return result
