from datetime import date
from decimal import Decimal

from django.db import migrations

# Gross hourly rates (TV-L SHK); the employer surcharge of 9.94 % is calibrated on SAP actuals
# (with bachelor, 8 h/week: 855.10 € per month).
RATES = [
    (False, "19.76", date(2026, 1, 1), date(2027, 3, 31)),
    (False, "20.67", date(2027, 4, 1), None),
    (True, "22.36", date(2026, 1, 1), date(2027, 3, 31)),
    (True, "23.27", date(2027, 4, 1), None),
]


def add_rates(apps, schema_editor):
    StudentHourlyRate = apps.get_model("staffing", "StudentHourlyRate")
    if StudentHourlyRate.objects.exists():
        return
    for has_bachelor, rate, valid_from, valid_until in RATES:
        StudentHourlyRate.objects.create(
            has_bachelor=has_bachelor, hourly_rate=Decimal(rate), employer_share=Decimal("9.94"),
            valid_from=valid_from, valid_until=valid_until,
        )


class Migration(migrations.Migration):
    dependencies = [("staffing", "0020_student_hourly_rate")]

    operations = [migrations.RunPython(add_rates, migrations.RunPython.noop)]
