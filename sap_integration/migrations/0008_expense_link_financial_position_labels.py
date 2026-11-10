from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sap_integration", "0007_sapexpenseunlinkarchive"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="sapexpenselink",
            options={
                "verbose_name": "SAP-Ausgabenverknüpfung (Finanzposition)",
                "verbose_name_plural": "SAP-Ausgabenverknüpfungen (Finanzposition)",
            },
        ),
        migrations.AlterModelOptions(
            name="sapexpenseunlinkarchive",
            options={
                "ordering": ("-created_at", "-pk"),
                "verbose_name": "Gelöste SAP-Ausgabenverknüpfung (Finanzposition)",
                "verbose_name_plural": "Gelöste SAP-Ausgabenverknüpfungen (Finanzposition)",
            },
        ),
    ]
