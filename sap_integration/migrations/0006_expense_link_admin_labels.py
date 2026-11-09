from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sap_integration", "0005_personnel_financial_position_categories"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="sapexpenselink",
            options={
                "verbose_name": "SAP-Ausgabenverknüpfung",
                "verbose_name_plural": "SAP-Ausgabenverknüpfungen",
            },
        ),
    ]
