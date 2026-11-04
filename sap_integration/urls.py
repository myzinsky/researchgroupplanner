from django.urls import path

from sap_integration import views
from sap_integration import expense_views


app_name = "sap_integration"

urlpatterns = [
    path("ausgaben/kategorien/", expense_views.categories, name="expense_categories"),
    path("ausgaben/kategorien/export/", expense_views.export_categories, name="expense_categories_export"),
    path("ausgaben/<int:fund_id>/", expense_views.expenses, name="expenses"),
    path("ausgaben/<int:fund_id>/<int:year>/<str:source_key>/", expense_views.expense_detail, name="expense_detail"),
    path("", views.overview, name="overview"),
    path("<int:year>/", views.overview, name="overview_year"),
    path("<int:year>/fonds/<int:fund_id>/", views.fund_detail, name="fund_detail"),
    path("import/", views.upload_export, name="upload_export"),
    path("abgleich/<int:fund_id>/", views.reconciliation, name="reconciliation"),
    path("abgleich/<int:fund_id>/zuordnung/", views.cost_type_mapping, name="cost_type_mapping"),
    path("abgleich/<int:fund_id>/ohne-beleg/loeschen/", views.delete_orphan, name="delete_orphan"),
    path(
        "abgleich/<int:fund_id>/position/<int:position_id>/",
        views.position_detail,
        name="position_detail",
    ),
]
