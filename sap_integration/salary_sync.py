import re
import unicodedata
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from projects.models import SAPFund
from sap_integration.cache import available_years, fund_values, load_year
from sap_integration.cleaning import clean_fund_values
from staffing.models import EmploymentSalaries, StaffMember


MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
MONTH_LABELS = {number: name.capitalize() for name, number in MONTHS.items()}
SALARY_POSITION_PATTERN = re.compile(
    rf"\bGehalt\s+({'|'.join(MONTHS)})\s+(20\d{{2}})\b",
    re.IGNORECASE,
)
COMMITMENT_POSITION_PATTERN = re.compile(
    r"\bMittelbindung\s+von\s+(\d{2})\.(\d{2})\.(20\d{2})"
    r"\s+bis\s+(\d{2})\.(\d{2})\.(20\d{2})\b",
    re.IGNORECASE,
)
CENT = Decimal("0.01")


@dataclass
class SalaryComparison:
    staff_member: StaffMember
    employment: object | None
    month: date
    sap_amount: Decimal
    planned: Decimal
    source: str
    can_apply: bool
    actual_amount: Decimal | None = None
    commitment_amount: Decimal | None = None
    blocking_reason: str | None = None

    @property
    def difference(self):
        return self.sap_amount - self.planned

    @property
    def source_label(self):
        return "SAP-Ist" if self.source == "actual" else "SAP-Obligo"

    @property
    def source_conflict(self):
        return (
            self.actual_amount is not None
            and self.commitment_amount is not None
            and self.actual_amount != self.commitment_amount
        )

    @property
    def month_label(self):
        return f"{MONTH_LABELS[self.month.month]} {self.month.year}"


@dataclass
class SalaryComparisonResult:
    comparisons: list[SalaryComparison]
    unmatched_partners: dict[str, int]
    ambiguous_partners: dict[str, int]


def build_salary_comparisons(data_dir):
    salary_values, display_names = _collect_salary_values(data_dir)
    staff_members = list(
        StaffMember.objects.prefetch_related(
            "employment_set__employmentsalaries_set"
        )
    )
    explicit_index, automatic_index = _staff_indexes(staff_members)
    comparisons = []
    unmatched = defaultdict(int)
    ambiguous = defaultdict(int)

    for (partner_key, month), amounts in sorted(
        salary_values.items(),
        key=lambda item: (item[0][1], item[0][0]),
    ):
        partner_name = display_names[partner_key]
        matches = explicit_index.get(partner_key) or automatic_index.get(partner_key, [])
        if not matches:
            unmatched[partner_name] += 1
            continue
        if len(matches) != 1:
            ambiguous[partner_name] += 1
            continue

        actual_amount = amounts.get("actual")
        commitment_amount = amounts.get("commitment")
        has_conflict = (
            actual_amount is not None
            and commitment_amount is not None
            and actual_amount.quantize(CENT) != commitment_amount.quantize(CENT)
        )
        if actual_amount is not None:
            sap_amount = actual_amount
            source = "actual"
        else:
            sap_amount = commitment_amount
            source = "commitment"

        comparisons.append(
            _comparison_for_staff(
                matches[0],
                month,
                sap_amount,
                source,
                actual_amount=actual_amount,
                commitment_amount=commitment_amount,
                source_conflict=has_conflict,
            )
        )

    return SalaryComparisonResult(
        comparisons=comparisons,
        unmatched_partners=dict(unmatched),
        ambiguous_partners=dict(ambiguous),
    )


def find_salary_comparison(data_dir, staff_member_id, year, month):
    target_month = date(year, month, 1)
    result = build_salary_comparisons(data_dir)
    return next(
        (
            comparison
            for comparison in result.comparisons
            if comparison.staff_member.id == staff_member_id
            and comparison.month == target_month
        ),
        None,
    )


def apply_salary_comparison(comparison):
    if not comparison.can_apply or comparison.employment is None:
        raise ValueError(
            comparison.blocking_reason
            or "Dieser SAP-Wert kann nicht sicher übernommen werden."
        )

    month_start = comparison.month
    month_end = date(
        month_start.year,
        month_start.month,
        monthrange(month_start.year, month_start.month)[1],
    )
    employment = comparison.employment
    salary_records = list(
        employment.employmentsalaries_set.filter(
            start_date__lte=month_end,
            end_date__gte=month_start,
        ).order_by("start_date", "end_date")
    )
    if len(salary_records) > 1:
        raise ValueError(
            "Der Monat enthält mehrere Gehaltssätze und kann nicht automatisch übernommen werden."
        )

    actual_start = max(month_start, employment.start_date)
    actual_end = min(month_end, employment.end_date)
    new_records = []

    if salary_records:
        existing = salary_records[0]
        if existing.start_date < actual_start:
            new_records.append(
                EmploymentSalaries(
                    employment=employment,
                    salary=existing.salary,
                    start_date=existing.start_date,
                    end_date=actual_start - timedelta(days=1),
                )
            )
        if existing.end_date > actual_end:
            after_record = EmploymentSalaries(
                employment=employment,
                salary=existing.salary,
                start_date=actual_end + timedelta(days=1),
                end_date=existing.end_date,
            )
        else:
            after_record = None
    else:
        existing = None
        after_record = None

    new_records.append(
        EmploymentSalaries(
            employment=employment,
            salary=comparison.sap_amount.quantize(CENT),
            start_date=actual_start,
            end_date=actual_end,
        )
    )
    if after_record is not None:
        new_records.append(after_record)

    with transaction.atomic():
        if existing is not None:
            existing.delete()
        EmploymentSalaries.objects.bulk_create(new_records)
        _merge_adjacent_salary_records(employment)


def _merge_adjacent_salary_records(employment):
    salary_records = list(
        employment.employmentsalaries_set.order_by("start_date", "end_date", "pk")
    )
    if not salary_records:
        return

    current = salary_records[0]
    for following in salary_records[1:]:
        is_adjacent = current.end_date + timedelta(days=1) == following.start_date
        if is_adjacent and current.salary == following.salary:
            current.end_date = following.end_date
            current.save(update_fields=["end_date"])
            following.delete()
        else:
            current = following


def _collect_salary_values(data_dir):
    funds = {
        fund.fund_number: fund
        for fund in SAPFund.objects.filter(is_active=True)
    }
    salary_values = defaultdict(dict)
    display_names = {}

    for year in available_years(data_dir):
        payload = load_year(data_dir, year)
        for fund_number, cached_fund in payload.get("funds", {}).items():
            fund = funds.get(fund_number)
            if fund is None:
                continue
            values = clean_fund_values(
                fund_values(cached_fund),
                fund.treat_negative_actuals_as_funding,
            )
            for row in values["transactions"]:
                if row.get("is_funding"):
                    continue
                salary_month = _salary_month(row)
                if salary_month is None:
                    continue
                partner_name = row.get("business_partner", "").strip()
                partner_key = normalize_person_name(partner_name)
                if not partner_key:
                    continue
                key = (partner_key, salary_month)
                row_type = row["type"]
                current = salary_values[key].get(row_type, Decimal("0"))
                salary_values[key][row_type] = current + Decimal(row["amount"])
                display_names.setdefault(partner_key, partner_name)

    return salary_values, display_names


def _salary_month(row):
    position = row.get("position", "")
    if row["type"] == "actual":
        match = SALARY_POSITION_PATTERN.search(position)
        if match is None:
            return None
        return date(
            int(match.group(2)),
            MONTHS[match.group(1).lower()],
            1,
        )

    if row["type"] != "commitment":
        return None
    match = COMMITMENT_POSITION_PATTERN.search(position)
    if match is None:
        return None
    start = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    end = date(int(match.group(6)), int(match.group(5)), int(match.group(4)))
    expected_end = date(start.year, start.month, monthrange(start.year, start.month)[1])
    if start.day != 1 or end != expected_end:
        return None
    return start


def _staff_indexes(staff_members):
    explicit = defaultdict(list)
    automatic = defaultdict(list)
    for staff_member in staff_members:
        automatic[
            normalize_person_name(
                f"{staff_member.first_name} {staff_member.last_name}"
            )
        ].append(staff_member)
        if staff_member.sap_business_partner.strip():
            explicit[
                normalize_person_name(staff_member.sap_business_partner)
            ].append(staff_member)
    return explicit, automatic


def _comparison_for_staff(
    staff_member,
    month,
    sap_amount,
    source,
    actual_amount=None,
    commitment_amount=None,
    source_conflict=False,
):
    month_end = date(month.year, month.month, monthrange(month.year, month.month)[1])
    employments = [
        employment
        for employment in staff_member.employment_set.all()
        if employment.start_date <= month_end and employment.end_date >= month
    ]
    if len(employments) != 1:
        reason = (
            "Für diesen Monat wurde keine passende Anstellung gefunden."
            if not employments
            else "Für diesen Monat wurden mehrere passende Anstellungen gefunden."
        )
        return SalaryComparison(
            staff_member,
            None,
            month,
            sap_amount.quantize(CENT),
            Decimal("0.00"),
            source,
            False,
            actual_amount=_quantize_optional(actual_amount),
            commitment_amount=_quantize_optional(commitment_amount),
            blocking_reason=reason,
        )

    employment = employments[0]
    salary_records = [
        salary
        for salary in employment.employmentsalaries_set.all()
        if salary.start_date <= month_end and salary.end_date >= month
    ]
    planned = sum((salary.salary for salary in salary_records), Decimal("0"))
    blocking_reason = None
    if employment.start_date > month or employment.end_date < month_end:
        blocking_reason = (
            "Teilmonate können nicht automatisch in die Planung übernommen werden."
        )
    elif len(salary_records) > 1:
        blocking_reason = (
            "Der Monat enthält mehrere Gehaltssätze. Bitte zuerst die Überlappung auflösen."
        )
    if source_conflict:
        blocking_reason = (
            "Für diesen Monat liegen unterschiedliche SAP-Ist- und "
            "SAP-Obligo-Werte vor. Eine automatische Übernahme ist nicht möglich."
        )

    return SalaryComparison(
        staff_member,
        employment,
        month,
        sap_amount.quantize(CENT),
        planned.quantize(CENT),
        source,
        blocking_reason is None,
        actual_amount=_quantize_optional(actual_amount),
        commitment_amount=_quantize_optional(commitment_amount),
        blocking_reason=blocking_reason,
    )


def _quantize_optional(value):
    return value.quantize(CENT) if value is not None else None


def normalize_person_name(value):
    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return tuple(sorted(re.findall(r"[a-z]+", ascii_value)))
