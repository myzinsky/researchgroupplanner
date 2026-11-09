from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("sap_integration", "0006_expense_link_admin_labels"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SAPExpenseUnlinkArchive",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_link_id", models.PositiveBigIntegerField(verbose_name="Ursprüngliche Verknüpfung")),
                ("fund_number", models.CharField(max_length=100, verbose_name="Fonds")),
                ("year", models.PositiveIntegerField(verbose_name="Jahr")),
                ("budget_label", models.TextField(verbose_name="Planungsbudget")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Gelöst am")),
                ("snapshot", models.JSONField(verbose_name="Archivdaten")),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name="Gelöst von")),
            ],
            options={
                "ordering": ("-created_at", "-pk"),
                "verbose_name": "Gelöste SAP-Ausgabenverknüpfung",
                "verbose_name_plural": "Gelöste SAP-Ausgabenverknüpfungen",
            },
        ),
    ]
