"""Theme validation and shared rendering with synthetic branding only."""

import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from controlling.theme import DEFAULT_PALETTE, contrast, theme_variables


class ThemeConfigurationTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "theme.json"
        override = override_settings(BRAND_THEME_FILE=self.path)
        override.enable()
        self.addCleanup(override.disable)

    def write(self, values):
        self.path.write_text(json.dumps(values))

    def test_missing_file_uses_semantic_defaults(self):
        values = theme_variables()
        for role, colour in DEFAULT_PALETTE.items():
            self.assertEqual(values[f"--gp-{role}"], colour)

    def test_partial_configuration_and_derived_colours_follow_primary(self):
        default_soft = theme_variables()["--gp-primary-soft"]
        self.write({"primary": "#246810"})
        values = theme_variables()
        self.assertEqual(values["--gp-primary"], "#246810")
        self.assertEqual(values["--gp-primary-rgb"], "36, 104, 16")
        self.assertNotEqual(values["--gp-primary-soft"], default_soft)
        self.assertEqual(values["--gp-success"], DEFAULT_PALETTE["success"])
        self.assertEqual(values["--gp-info"], "#246810")

    def test_reloading_after_edits_and_removal(self):
        self.write({"primary": "#123456"})
        self.assertEqual(theme_variables()["--gp-primary"], "#123456")
        self.write({"primary": "#ABCDEF", "note": "#EFEFEF"})
        self.assertEqual(theme_variables()["--gp-primary"], "#ABCDEF")
        self.path.unlink()
        self.assertEqual(theme_variables()["--gp-primary"], DEFAULT_PALETTE["primary"])

    def test_invalid_json_unknown_roles_and_css_injection_fall_back(self):
        for index, values in enumerate(([], {"blue": "#123456"}, {"primary": None},
                                        {"primary": "red; } body { display:none }"},
                                        {"primary": "#123"})):
            with self.subTest(values=values):
                self.path = self.path.with_name(f"invalid-{index}.json")
                self.write(values)
                with override_settings(BRAND_THEME_FILE=self.path), self.assertLogs("controlling.theme", level="WARNING"):
                    self.assertEqual(theme_variables()["--gp-primary"], DEFAULT_PALETTE["primary"])
        self.path = self.path.with_name("broken.json")
        self.path.write_text("not-json")
        with override_settings(BRAND_THEME_FILE=self.path), self.assertLogs("controlling.theme", level="WARNING"):
            self.assertEqual(theme_variables()["--gp-primary"], DEFAULT_PALETTE["primary"])

    def test_oversized_configuration_is_not_rendered(self):
        self.path.write_text(" " * 32769)
        with self.assertLogs("controlling.theme", level="WARNING"):
            self.assertEqual(theme_variables()["--gp-primary"], DEFAULT_PALETTE["primary"])

    def test_default_button_text_and_hover_text_have_sufficient_contrast(self):
        for primary in (DEFAULT_PALETTE["primary"], "#777777", "#EEEEEE"):
            self.write({"primary": primary})
            values = theme_variables()
            for role in ("primary", "secondary", "success", "danger", "warning"):
                for suffix in ("", "-hover"):
                    with self.subTest(primary=primary, role=role, suffix=suffix):
                        self.assertGreaterEqual(contrast(values[f"--gp-{role}{suffix}"],
                                                         values[f"--gp-on-{role}{suffix}"]), 4.5)


@override_settings(SAP_ENABLED=False, SAP_GM_IMPORT_ENABLED=False, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ThemeRenderingTests(TestCase):
    def test_deployment_palette_reaches_home_statistics_and_admin(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "theme.json"
        path.write_text(json.dumps({"primary": "#246810"}))
        user = get_user_model().objects.create_superuser("theme-admin", password="synthetic")
        self.client.force_login(user)
        with override_settings(BRAND_THEME_FILE=path):
            for name in ("main", "statistics", "admin:index"):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "--gp-primary: #246810;")
                self.assertContains(response, "brand-theme-variables")
            self.assertContains(response, "controlling/admin-theme.css")
