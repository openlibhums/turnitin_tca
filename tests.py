import base64
import hashlib
import hmac
import importlib
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, SimpleTestCase, RequestFactory, override_settings
from django.urls import clear_url_caches, reverse
from django.utils import timezone

from core import models as core_models
from utils import setting_handler
from utils.testing import helpers

from plugins.turnitin_tca import client, forms, hooks, logic, models
from plugins.turnitin_tca import plugin_settings as tca_settings


WEBHOOK_SECRET_RAW = b"unit-test-secret"
WEBHOOK_SECRET_B64 = base64.b64encode(WEBHOOK_SECRET_RAW).decode("ascii")


def configure_journal(journal, **overrides):
    plugin = tca_settings.TurnitinTCAPlugin.get_self()
    values = {
        "turnitin_enabled": True,
        "turnitin_api_url": "https://tenant.turnitin.com",
        "turnitin_api_key": "secret-key",
        "turnitin_webhook_secret": WEBHOOK_SECRET_B64,
        # Known up front so tests do not make a Get Features Enabled call.
        "turnitin_require_eula": "false",
    }
    values.update(overrides)
    for name, value in values.items():
        setting_handler.save_plugin_setting(plugin, name, value, journal)


def sign(body):
    return hmac.new(
        WEBHOOK_SECRET_RAW,
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def add_manuscript_file(article, label="Manuscript"):
    file_obj = core_models.File.objects.create(
        article_id=article.pk,
        label=label,
        original_filename="manuscript.docx",
        uuid_filename="abc.docx",
        mime_type="application/octet-stream",
    )
    article.manuscript_files.add(file_obj)
    return file_obj


class TurnitinClientTest(SimpleTestCase):
    def setUp(self):
        self.client = client.TurnitinClient(
            base_url="https://tenant.turnitin.com/",
            api_key="key",
            integration_name="Janeway",
            integration_version="1.0",
        )

    def test_url_building_strips_and_joins(self):
        self.assertEqual(
            self.client.url("submissions"),
            "https://tenant.turnitin.com/api/v1/submissions",
        )

    def test_headers_include_required_metadata(self):
        headers = self.client.headers()
        self.assertEqual(headers["Authorization"], "Bearer key")
        self.assertEqual(headers["X-Turnitin-Integration-Name"], "Janeway")
        self.assertEqual(headers["X-Turnitin-Integration-Version"], "1.0")


@override_settings(URL_CONFIG="domain")
class TurnitinLogicTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        helpers.create_press()
        cls.journal_one, cls.journal_two = helpers.create_journals()
        helpers.create_roles(["editor", "author"])
        tca_settings.install()
        cls.editor = helpers.create_editor(cls.journal_one)
        cls.article = helpers.create_article(cls.journal_one)
        cls.file = add_manuscript_file(cls.article)
        configure_journal(cls.journal_one)

    def test_is_enabled_true_when_configured(self):
        self.assertTrue(logic.is_enabled(self.journal_one))

    def test_is_enabled_false_when_unconfigured(self):
        self.assertFalse(logic.is_enabled(self.journal_two))

    def test_get_article_files_includes_manuscript(self):
        files = logic.get_article_files(self.article)
        labels = {entry["file"].pk: entry["kind"] for entry in files}
        self.assertIn(self.file.pk, labels)
        self.assertEqual(labels[self.file.pk], "Manuscript")

    def test_verify_webhook_signature(self):
        body = b'{"id": "abc"}'
        good = hmac.new(
            WEBHOOK_SECRET_RAW,
            body,
            hashlib.sha256,
        ).hexdigest()
        self.assertTrue(
            logic.verify_webhook_signature(self.journal_one, body, good),
        )
        self.assertFalse(
            logic.verify_webhook_signature(self.journal_one, body, "bad"),
        )

    def test_signing_key_decodes_base64_secret(self):
        self.assertEqual(logic.signing_key(WEBHOOK_SECRET_B64), WEBHOOK_SECRET_RAW)

    def test_generate_signing_secret_is_base64(self):
        secret = logic.generate_signing_secret()
        self.assertEqual(len(base64.b64decode(secret, validate=True)), 32)

    def test_submission_complete_requests_report(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-1",
            status="PROCESSING",
            requested_by=self.editor,
        )
        fake_client = mock.Mock()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.handle_webhook_event(
                "SUBMISSION_COMPLETE",
                {"id": "tii-1", "status": "COMPLETE"},
                self.journal_one,
            )
        submission.refresh_from_db()
        self.assertEqual(submission.status, "COMPLETE")
        self.assertTrue(submission.report_requested)
        self.assertTrue(fake_client.generate_similarity_report.called)

    def test_similarity_complete_sets_score_and_emails(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-2",
            status="COMPLETE",
            report_requested=True,
            report_status="PROCESSING",
            requested_by=self.editor,
        )
        with mock.patch.object(
            logic.notify_helpers,
            "send_email_with_body_from_user",
        ) as send_mail:
            logic.handle_webhook_event(
                "SIMILARITY_COMPLETE",
                {"id": "tii-2", "status": "COMPLETE", "overall_match_percentage": 42},
                self.journal_one,
            )
        submission.refresh_from_db()
        self.assertEqual(submission.report_status, "COMPLETE")
        self.assertEqual(submission.similarity_score, 42)
        self.assertTrue(send_mail.called)

    def test_webhook_event_ignores_other_journals_submission(self):
        # A submission that belongs to journal_two must not be mutated by a
        # webhook processed in journal_one's context, even if the id matches.
        other_article = helpers.create_article(self.journal_two)
        submission = models.TurnitinSubmission.objects.create(
            article=other_article,
            tii_submission_id="tii-cross",
            status="PROCESSING",
            report_status="PROCESSING",
            requested_by=self.editor,
        )
        with mock.patch.object(
            logic.notify_helpers,
            "send_email_with_body_from_user",
        ) as send_mail:
            result = logic.handle_webhook_event(
                "SIMILARITY_COMPLETE",
                {
                    "id": "tii-cross",
                    "status": "COMPLETE",
                    "overall_match_percentage": 88,
                },
                self.journal_one,
            )
        self.assertIsNone(result)
        submission.refresh_from_db()
        self.assertEqual(submission.report_status, "PROCESSING")
        self.assertIsNone(submission.similarity_score)
        self.assertFalse(send_mail.called)

    def test_report_completion_emails_only_once(self):
        # A duplicate webhook (or an overlapping poll) must not email twice.
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-dup",
            status="COMPLETE",
            report_requested=True,
            report_status="PROCESSING",
            requested_by=self.editor,
        )
        payload = {
            "id": "tii-dup",
            "status": "COMPLETE",
            "overall_match_percentage": 30,
        }
        with mock.patch.object(
            logic.notify_helpers,
            "send_email_with_body_from_user",
        ) as send_mail:
            logic.handle_webhook_event(
                "SIMILARITY_COMPLETE",
                payload,
                self.journal_one,
            )
            logic.handle_webhook_event(
                "SIMILARITY_COMPLETE",
                payload,
                self.journal_one,
            )
        self.assertEqual(send_mail.call_count, 1)
        submission.refresh_from_db()
        self.assertTrue(submission.report_notified)

    def test_apply_report_result_does_not_regress_completed_report(self):
        # A stale observation (e.g. a poll that started before a webhook
        # completed the report) must not overwrite a COMPLETE report.
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-stale",
            status="COMPLETE",
            report_requested=True,
            report_status="COMPLETE",
            similarity_score=55,
            report_notified=True,
        )
        logic.apply_report_result(submission, "PROCESSING", None)
        submission.refresh_from_db()
        self.assertEqual(submission.report_status, "COMPLETE")
        self.assertEqual(submission.similarity_score, 55)

    def test_processing_report_does_not_store_a_score(self):
        # Turnitin reports 0% while a report is still being generated.
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-processing",
            status="COMPLETE",
            report_requested=True,
            report_status="PROCESSING",
        )
        logic.apply_report_result(submission, "PROCESSING", 0)
        submission.refresh_from_db()
        self.assertIsNone(submission.similarity_score)

    def test_similarity_webhook_records_last_webhook(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-last-webhook",
            status="COMPLETE",
            report_requested=True,
            report_status="PROCESSING",
            report_notified=True,
        )
        logic.handle_webhook_event(
            "SIMILARITY_COMPLETE",
            {
                "id": "tii-last-webhook",
                "status": "COMPLETE",
                "overall_match_percentage": 9,
            },
            self.journal_one,
        )
        submission.refresh_from_db()
        self.assertIsNotNone(submission.last_webhook)

    def test_similarity_updated_overwrites_completed_score(self):
        # Saved viewer changes alter a report that is already complete.
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-updated",
            status="COMPLETE",
            report_requested=True,
            report_status="COMPLETE",
            similarity_score=55,
            report_notified=True,
            requested_by=self.editor,
        )
        with mock.patch.object(
            logic.notify_helpers,
            "send_email_with_body_from_user",
        ) as send_mail:
            logic.handle_webhook_event(
                "SIMILARITY_UPDATED",
                {
                    "id": "tii-updated",
                    "status": "COMPLETE",
                    "overall_match_percentage": 12,
                },
                self.journal_one,
            )
        submission.refresh_from_db()
        self.assertEqual(submission.similarity_score, 12)
        send_mail.assert_not_called()


@override_settings(URL_CONFIG="domain")
class TurnitinHookTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        helpers.create_press()
        cls.journal_one, cls.journal_two = helpers.create_journals()
        helpers.create_roles(["editor", "author"])
        tca_settings.install()
        clear_url_caches()
        importlib.reload(importlib.import_module(settings.ROOT_URLCONF))
        importlib.reload(importlib.import_module("core.include_urls"))
        cls.article = helpers.create_article(cls.journal_one)
        configure_journal(cls.journal_one)

    def make_context(self, journal):
        request = RequestFactory().get("/")
        request.journal = journal
        return {"request": request, "article": self.article}

    def test_hook_renders_link_when_enabled(self):
        output = hooks.review_action_hook(self.make_context(self.journal_one))
        self.assertIn("Turnitin Similarity", output)
        self.assertIn(
            reverse("turnitin_tca_article", kwargs={"article_id": self.article.pk}),
            output,
        )

    def test_hook_empty_when_disabled(self):
        output = hooks.review_action_hook(self.make_context(self.journal_two))
        self.assertEqual(output, "")


@override_settings(URL_CONFIG="domain")
class TurnitinViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        helpers.create_press()
        cls.journal_one, cls.journal_two = helpers.create_journals()
        helpers.create_roles(["editor", "author"])
        tca_settings.install()
        clear_url_caches()
        importlib.reload(importlib.import_module(settings.ROOT_URLCONF))
        importlib.reload(importlib.import_module("core.include_urls"))
        cls.editor = helpers.create_editor(cls.journal_one)
        cls.author = helpers.create_author(cls.journal_one)
        cls.article = helpers.create_article(cls.journal_one)
        cls.file = add_manuscript_file(cls.article)
        cls.other_article = helpers.create_article(cls.journal_two)
        configure_journal(cls.journal_one)

    def test_article_page_requires_login(self):
        response = self.client.get(
            reverse("turnitin_tca_article", kwargs={"article_id": self.article.pk}),
            SERVER_NAME=self.journal_one.domain,
        )
        self.assertEqual(response.status_code, 302)

    def test_article_page_renders_for_editor(self):
        self.client.force_login(self.editor)
        response = self.client.get(
            reverse("turnitin_tca_article", kwargs={"article_id": self.article.pk}),
            SERVER_NAME=self.journal_one.domain,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.file.pk, [r["file"].pk for r in response.context["rows"]])

    def test_article_page_denied_to_non_editor(self):
        self.client.force_login(self.author)
        response = self.client.get(
            reverse("turnitin_tca_article", kwargs={"article_id": self.article.pk}),
            SERVER_NAME=self.journal_one.domain,
        )
        self.assertNotEqual(response.status_code, 200)

    def test_article_page_scoped_to_current_journal(self):
        # An editor of journal_one cannot reach a journal_two article through
        # journal_one's host, even though the article exists.
        self.client.force_login(self.editor)
        response = self.client.get(
            reverse(
                "turnitin_tca_article",
                kwargs={"article_id": self.other_article.pk},
            ),
            SERVER_NAME=self.journal_one.domain,
        )
        self.assertEqual(response.status_code, 404)

    def test_manager_renders_and_saves_settings(self):
        self.client.force_login(self.editor)
        url = reverse("turnitin_tca_manager")
        get_response = self.client.get(url, SERVER_NAME=self.journal_one.domain)
        self.assertEqual(get_response.status_code, 200)

        post_response = self.client.post(
            url,
            data={
                "turnitin_enabled": "on",
                "turnitin_api_url": "https://saved.turnitin.com",
                "turnitin_api_key": "saved-key",
            },
            SERVER_NAME=self.journal_one.domain,
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(
            logic.get_setting("turnitin_api_url", self.journal_one),
            "https://saved.turnitin.com",
        )

    def test_send_file_creates_submission(self):
        self.client.force_login(self.editor)
        with mock.patch.object(
            logic,
            "send_file_for_report",
        ) as send:
            response = self.client.post(
                reverse(
                    "turnitin_tca_send_file",
                    kwargs={"article_id": self.article.pk, "file_id": self.file.pk},
                ),
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(send.called)

    def test_webhook_rejects_bad_signature(self):
        response = self.client.post(
            reverse("turnitin_tca_webhook"),
            data=json.dumps({"id": "x"}),
            content_type="application/json",
            HTTP_X_TURNITIN_SIGNATURE="bad",
            HTTP_X_TURNITIN_EVENTTYPE="SIMILARITY_COMPLETE",
            SERVER_NAME=self.journal_one.domain,
        )
        self.assertEqual(response.status_code, 403)

    def test_webhook_accepts_valid_signature(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-hook",
            status="COMPLETE",
            report_requested=True,
            report_status="PROCESSING",
            requested_by=self.editor,
        )
        payload = {
            "id": "tii-hook",
            "status": "COMPLETE",
            "overall_match_percentage": 17,
        }
        body = json.dumps(payload)
        with mock.patch.object(
            logic.notify_helpers,
            "send_email_with_body_from_user",
        ):
            response = self.client.post(
                reverse("turnitin_tca_webhook"),
                data=body,
                content_type="application/json",
                HTTP_X_TURNITIN_SIGNATURE=sign(body),
                HTTP_X_TURNITIN_EVENTTYPE="SIMILARITY_COMPLETE",
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertEqual(response.status_code, 200)
        submission.refresh_from_db()
        self.assertEqual(submission.similarity_score, 17)

    def test_register_webhook_posts_documented_payload(self):
        fake_client = mock.Mock()
        fake_client.create_webhook.return_value = {"id": "wh-123"}
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            result = logic.register_webhook(self.journal_one)
        self.assertEqual(result["id"], "wh-123")
        fake_client.delete_webhook.assert_not_called()
        payload = fake_client.create_webhook.call_args[0][0]
        self.assertEqual(payload["event_types"], tca_settings.SUBSCRIBED_EVENTS)
        self.assertFalse(payload["allow_insecure"])
        self.assertTrue(payload["url"].endswith("/webhook/"))
        # signing_secret must be valid Base64 per the TCA docs.
        base64.b64decode(payload["signing_secret"], validate=True)
        # The returned id is persisted for later re-registration.
        self.assertEqual(
            logic.get_setting("turnitin_webhook_id", self.journal_one),
            "wh-123",
        )

    def test_register_webhook_uses_url_override(self):
        override = (
            "https://abc123.ngrok-free.app/olh/plugins/turnitin-core-api/webhook/"
        )
        configure_journal(self.journal_one, turnitin_webhook_url=override)
        fake_client = mock.Mock()
        fake_client.create_webhook.return_value = {"id": "wh-ngrok"}
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.register_webhook(self.journal_one)
        payload = fake_client.create_webhook.call_args[0][0]
        self.assertEqual(payload["url"], override)
        configure_journal(self.journal_one, turnitin_webhook_url="")

    def test_register_webhook_deletes_previous_registration(self):
        setting_handler.save_plugin_setting(
            logic.get_plugin(),
            "turnitin_webhook_id",
            "old-id",
            self.journal_one,
        )
        fake_client = mock.Mock()
        fake_client.create_webhook.return_value = {"id": "wh-new"}
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.register_webhook(self.journal_one)
        fake_client.delete_webhook.assert_called_once_with("old-id")

    def test_delete_webhook_clears_registered_id(self):
        setting_handler.save_plugin_setting(
            logic.get_plugin(),
            "turnitin_webhook_id",
            "wh-1",
            self.journal_one,
        )
        fake_client = mock.Mock()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.delete_webhook(self.journal_one, "wh-1")
        fake_client.delete_webhook.assert_called_once_with("wh-1")
        self.assertEqual(
            logic.get_setting("turnitin_webhook_id", self.journal_one),
            "",
        )

    def test_delete_all_webhooks(self):
        fake_client = mock.Mock()
        fake_client.list_webhooks.return_value = [{"id": "a"}, {"id": "b"}]
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            count = logic.delete_all_webhooks(self.journal_one)
        self.assertEqual(count, 2)
        self.assertEqual(fake_client.delete_webhook.call_count, 2)

    def test_webhooks_page_lists_and_deletes(self):
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        fake_client.list_webhooks.return_value = [
            {
                "id": "wh-x",
                "url": "https://x/",
                "event_types": ["SUBMISSION_COMPLETE"],
                "created_time": "2026-01-01T00:00:00Z",
            },
        ]
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            page = self.client.get(
                reverse("turnitin_tca_webhooks"),
                SERVER_NAME=self.journal_one.domain,
            )
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "wh-x")
            # First click without confirmation shows the modal, deletes nothing.
            unconfirmed = self.client.post(
                reverse("turnitin_tca_delete_webhook", kwargs={"webhook_id": "wh-x"}),
                data={forms.DeleteWebhookForm.CONFIRMABLE_BUTTON_NAME: "1"},
                SERVER_NAME=self.journal_one.domain,
            )
            self.assertEqual(unconfirmed.status_code, 200)
            fake_client.delete_webhook.assert_not_called()
            response = self.client.post(
                reverse("turnitin_tca_delete_webhook", kwargs={"webhook_id": "wh-x"}),
                data={forms.DeleteWebhookForm.CONFIRMED_BUTTON_NAME: "1"},
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertEqual(response.status_code, 302)
        fake_client.delete_webhook.assert_called_once_with("wh-x")

    def test_regenerate_report_shows_confirmation_modal(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-confirm",
            status="COMPLETE",
            report_requested=True,
            report_status="COMPLETE",
            similarity_score=12,
            requested_by=self.editor,
        )
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            response = self.client.post(
                reverse(
                    "turnitin_tca_regenerate_report",
                    kwargs={
                        "article_id": self.article.pk,
                        "submission_id": submission.pk,
                    },
                ),
                data={forms.RegenerateReportForm.CONFIRMABLE_BUTTON_NAME: "1"},
                SERVER_NAME=self.journal_one.domain,
            )
        # The first click renders the page with the confirmation modal and
        # does not call the API.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "confirm_modal")
        fake_client.generate_similarity_report.assert_not_called()

    def test_regenerate_report_requests_new_report_when_confirmed(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-regen",
            status="COMPLETE",
            report_requested=True,
            report_status="COMPLETE",
            similarity_score=12,
            requested_by=self.editor,
        )
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            response = self.client.post(
                reverse(
                    "turnitin_tca_regenerate_report",
                    kwargs={
                        "article_id": self.article.pk,
                        "submission_id": submission.pk,
                    },
                ),
                data={forms.RegenerateReportForm.CONFIRMED_BUTTON_NAME: "1"},
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertEqual(response.status_code, 302)
        fake_client.generate_similarity_report.assert_called_once()
        submission.refresh_from_db()
        self.assertEqual(submission.report_status, "PROCESSING")

    def test_regenerate_report_requires_submission(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="",
            status="CREATED",
            requested_by=self.editor,
        )
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            response = self.client.post(
                reverse(
                    "turnitin_tca_regenerate_report",
                    kwargs={
                        "article_id": self.article.pk,
                        "submission_id": submission.pk,
                    },
                ),
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertEqual(response.status_code, 302)
        fake_client.generate_similarity_report.assert_not_called()

    def test_send_file_requires_eula_when_tenant_requires_it(self):
        configure_journal(self.journal_one, turnitin_require_eula="true")
        self.client.force_login(self.editor)
        with mock.patch.object(
            logic,
            "get_latest_eula",
            return_value={"version": "v1"},
        ):
            response = self.client.post(
                reverse(
                    "turnitin_tca_send_file",
                    kwargs={"article_id": self.article.pk, "file_id": self.file.pk},
                ),
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertRedirects(
            response,
            reverse("turnitin_tca_eula", kwargs={"article_id": self.article.pk}),
            target_status_code=200,
            fetch_redirect_response=False,
        )
        # Reset so other tests see EULA off again.
        configure_journal(self.journal_one, turnitin_require_eula="false")

    def test_saving_settings_checks_features_and_stores_require_eula(self):
        configure_journal(self.journal_one, turnitin_require_eula="")
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        fake_client.features_enabled.return_value = {
            "product_name": "iThenticate",
            "tenant": {"require_eula": True},
        }
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            self.client.post(
                reverse("turnitin_tca_manager"),
                data={
                    "turnitin_enabled": "on",
                    "turnitin_api_url": "https://tenant.turnitin.com",
                    "turnitin_api_key": "secret-key",
                },
                SERVER_NAME=self.journal_one.domain,
            )
        fake_client.features_enabled.assert_called_once()
        self.assertEqual(
            logic.get_setting("turnitin_require_eula", self.journal_one),
            "true",
        )
        configure_journal(self.journal_one, turnitin_require_eula="false")

    def test_failed_connection_check_shows_friendly_message(self):
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        fake_client.features_enabled.side_effect = client.TurnitinError(
            "TCA GET features-enabled returned 401",
            status_code=401,
            body="raw api detail",
        )
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            response = self.client.get(
                reverse("turnitin_tca_test_connection"),
                SERVER_NAME=self.journal_one.domain,
                follow=True,
            )
        self.assertContains(response, "Could not connect to Turnitin")
        self.assertNotContains(response, "raw api detail")
        configure_journal(self.journal_one, turnitin_require_eula="false")


class TurnitinCronInstallTest(SimpleTestCase):
    def make_tab(self, existing_jobs=()):
        tab = mock.MagicMock()
        tab.__iter__.return_value = iter(existing_jobs)
        return tab

    @override_settings(IN_TEST_RUNNER=False)
    def test_install_cron_creates_job_on_poll_interval(self):
        tab = self.make_tab()
        job = mock.Mock()
        tab.new.return_value = job
        with (
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("crontab.CronTab", return_value=tab),
        ):
            tca_settings.install_cron()
        tab.new.assert_called_once()
        self.assertEqual(
            tab.new.call_args.kwargs["comment"],
            tca_settings.cron_job_name(),
        )
        job.minute.every.assert_called_once_with(tca_settings.POLL_EVERY_MINUTES)
        tab.write.assert_called_once()

    @override_settings(IN_TEST_RUNNER=False)
    def test_install_cron_is_idempotent(self):
        existing = mock.Mock()
        existing.comment = tca_settings.cron_job_name()
        tab = self.make_tab(existing_jobs=[existing])
        with (
            mock.patch("os.path.isfile", return_value=True),
            mock.patch("crontab.CronTab", return_value=tab),
        ):
            tca_settings.install_cron()
        tab.new.assert_not_called()
        tab.write.assert_not_called()

    def test_install_cron_skipped_under_test_runner(self):
        with mock.patch("crontab.CronTab") as cron_tab:
            tca_settings.install_cron()
        cron_tab.assert_not_called()


@override_settings(URL_CONFIG="domain")
class TurnitinSendFlowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        helpers.create_press()
        cls.journal_one, cls.journal_two = helpers.create_journals()
        helpers.create_roles(["editor", "author"])
        tca_settings.install()
        cls.editor = helpers.create_editor(cls.journal_one)
        cls.article = helpers.create_article(cls.journal_one)
        cls.file = add_manuscript_file(cls.article)
        configure_journal(cls.journal_one)

    def test_send_file_for_report_creates_and_uploads(self):
        fake_client = mock.Mock()
        fake_client.create_submission.return_value = {
            "id": "tii-flow",
            "status": "CREATED",
        }
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            submission = logic.send_file_for_report(
                self.article,
                self.file,
                self.editor,
            )
        self.assertEqual(submission.tii_submission_id, "tii-flow")
        self.assertEqual(submission.status, "PROCESSING")
        fake_client.create_submission.assert_called_once()
        fake_client.upload_file.assert_called_once()
        payload = fake_client.create_submission.call_args[0][0]
        self.assertEqual(payload["owner_default_permission_set"], "UNDEFINED")
        self.assertEqual(payload["submitter_default_permission_set"], "EDITOR")
        self.assertIn("metadata", payload)

    def test_payload_without_correspondence_author_is_anonymous(self):
        self.article.correspondence_author = None
        payload = logic.build_submission_payload(
            self.article,
            self.file,
            self.editor,
        )
        owner_id = payload["owner"]
        self.assertNotEqual(owner_id, logic.account_external_id(self.editor))
        self.assertEqual(
            payload["metadata"]["owners"],
            [{"id": owner_id, "given_name": "Anonymous", "family_name": "Author"}],
        )
        self.assertEqual(str(uuid.UUID(owner_id)), owner_id)

    def test_payload_owner_is_correspondence_author(self):
        author = helpers.create_author(self.journal_one)
        self.article.correspondence_author = author
        payload = logic.build_submission_payload(
            self.article,
            self.file,
            self.editor,
        )
        self.assertEqual(payload["owner"], logic.account_external_id(author))
        self.assertEqual(
            payload["metadata"]["owners"][0]["email"],
            author.email,
        )

    def test_send_file_records_error_on_failure(self):
        fake_client = mock.Mock()
        fake_client.create_submission.side_effect = client.TurnitinError(
            "boom",
            status_code=400,
        )
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            with self.assertRaises(client.TurnitinError):
                logic.send_file_for_report(
                    self.article,
                    self.file,
                    self.editor,
                )
        submission = models.TurnitinSubmission.objects.latest("date_created")
        self.assertEqual(submission.status, "ERROR")
        self.assertIn("boom", submission.error_message)

    def test_get_viewer_url(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-view",
            report_status="COMPLETE",
        )
        fake_client = mock.Mock()
        fake_client.viewer_url.return_value = {"viewer_url": "https://viewer"}
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            url = logic.get_viewer_url(submission, self.editor)
        self.assertEqual(url, "https://viewer")
        payload = fake_client.viewer_url.call_args[0][1]
        self.assertEqual(payload["viewer_default_permission_set"], "EDITOR")
        # On by default, so SIMILARITY_UPDATED keeps the score current.
        self.assertTrue(payload["similarity"]["view_settings"]["save_changes"])
        self.assertNotIn("eula", payload)

    def test_get_viewer_url_sends_save_changes_setting(self):
        configure_journal(self.journal_one, turnitin_save_report_changes=False)
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-save",
            report_status="COMPLETE",
        )
        fake_client = mock.Mock()
        fake_client.viewer_url.return_value = {"viewer_url": "https://viewer"}
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.get_viewer_url(submission, self.editor)
        payload = fake_client.viewer_url.call_args[0][1]
        self.assertFalse(payload["similarity"]["view_settings"]["save_changes"])
        configure_journal(self.journal_one, turnitin_save_report_changes=True)

    def test_eula_required_follows_stored_tenant_value(self):
        configure_journal(self.journal_one, turnitin_require_eula="true")
        self.assertTrue(logic.eula_required(self.journal_one))
        configure_journal(self.journal_one, turnitin_require_eula="false")
        self.assertFalse(logic.eula_required(self.journal_one))

    def test_eula_required_fetches_features_when_unknown(self):
        configure_journal(self.journal_one, turnitin_require_eula="")
        fake_client = mock.Mock()
        fake_client.features_enabled.return_value = {
            "tenant": {"require_eula": True},
        }
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            self.assertTrue(logic.eula_required(self.journal_one))
        self.assertEqual(
            logic.get_setting("turnitin_require_eula", self.journal_one),
            "true",
        )
        configure_journal(self.journal_one, turnitin_require_eula="false")

    def test_eula_required_falls_back_when_features_unavailable(self):
        # The send goes ahead; a tenant that needs the EULA answers 451.
        configure_journal(self.journal_one, turnitin_require_eula="")
        fake_client = mock.Mock()
        fake_client.features_enabled.side_effect = outage()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            self.assertFalse(logic.eula_required(self.journal_one))
        self.assertEqual(
            logic.get_setting("turnitin_require_eula", self.journal_one),
            "",
        )
        configure_journal(self.journal_one, turnitin_require_eula="false")

    def test_build_eula_block_records_acceptance(self):
        block = logic.build_eula_block(self.editor, "v9")
        self.assertEqual(block["version"], "v9")
        self.assertTrue(
            models.TurnitinEulaAcceptance.objects.filter(
                account=self.editor,
                version="v9",
            ).exists(),
        )

    def test_poll_command_advances_pending_submission(self):
        submission = models.TurnitinSubmission.objects.create(
            article=self.article,
            file=self.file,
            tii_submission_id="tii-poll",
            status="PROCESSING",
            requested_by=self.editor,
        )
        # Age it past the 30 minute poll threshold.
        models.TurnitinSubmission.objects.filter(pk=submission.pk).update(
            date_created=timezone.now() - timedelta(hours=1),
        )
        fake_client = mock.Mock()
        fake_client.submission_info.return_value = {"status": "COMPLETE"}
        fake_client.similarity_report_info.return_value = {
            "status": "COMPLETE",
            "overall_match_percentage": 9,
        }
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch.object(
                logic.notify_helpers,
                "send_email_with_body_from_user",
            ),
        ):
            call_command("poll_turnitin_tca")
        submission.refresh_from_db()
        self.assertEqual(submission.status, "COMPLETE")
        self.assertTrue(submission.report_requested)
        self.assertEqual(submission.similarity_score, 9)


def eula_refusal():
    return client.TurnitinError("EULA", status_code=451)


def outage():
    return client.TurnitinError("TCA request failed: connection refused")


class TurnitinClientRetryTest(SimpleTestCase):
    def setUp(self):
        self.client = client.TurnitinClient(
            base_url="https://tenant.turnitin.com",
            api_key="key",
        )

    def make_response(self, status_code, headers=None):
        response = mock.Mock()
        response.status_code = status_code
        response.headers = headers or {}
        response.text = ""
        return response

    def test_transient_errors(self):
        self.assertTrue(client.TurnitinError("x").transient)
        self.assertTrue(client.TurnitinError("x", status_code=429).transient)
        self.assertTrue(client.TurnitinError("x", status_code=503).transient)
        self.assertFalse(client.TurnitinError("x", status_code=400).transient)
        self.assertFalse(eula_refusal().transient)
        self.assertTrue(eula_refusal().eula_required)

    def test_rate_limit_is_retried_honouring_retry_after(self):
        responses = [
            self.make_response(429, {"Retry-After": "7"}),
            self.make_response(200),
        ]
        with (
            mock.patch("requests.request", side_effect=responses) as request,
            mock.patch("time.sleep") as sleep,
        ):
            self.client.request("GET", "features-enabled")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(7)

    def test_client_error_is_not_retried(self):
        with (
            mock.patch(
                "requests.request",
                return_value=self.make_response(400),
            ) as request,
            mock.patch("time.sleep") as sleep,
        ):
            with self.assertRaises(client.TurnitinError) as raised:
                self.client.request("GET", "features-enabled")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(raised.exception.status_code, 400)

    def test_single_attempt_does_not_sleep_on_network_error(self):
        self.client.max_retries = 1
        import requests

        with (
            mock.patch(
                "requests.request",
                side_effect=requests.ConnectionError("down"),
            ),
            mock.patch("time.sleep") as sleep,
        ):
            with self.assertRaises(client.TurnitinError) as raised:
                self.client.request("GET", "features-enabled")
        sleep.assert_not_called()
        self.assertTrue(raised.exception.transient)


@override_settings(URL_CONFIG="domain")
class TurnitinRetryTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        helpers.create_press()
        cls.journal_one, cls.journal_two = helpers.create_journals()
        helpers.create_roles(["editor", "author"])
        tca_settings.install()
        cls.editor = helpers.create_editor(cls.journal_one)
        cls.article = helpers.create_article(cls.journal_one)
        cls.file = add_manuscript_file(cls.article)
        cls.second_file = add_manuscript_file(cls.article, label="Second")
        configure_journal(cls.journal_one)
        past = timezone.now() - timedelta(minutes=1)
        cls.upload_pending = models.TurnitinSubmission.objects.create(
            article=cls.article,
            file=cls.file,
            tii_submission_id="tii-upload-pending",
            status="RETRYING",
            send_attempts=1,
            next_attempt_at=past,
            requested_by=cls.editor,
        )
        cls.create_pending = models.TurnitinSubmission.objects.create(
            article=cls.article,
            file=cls.second_file,
            status="RETRYING",
            send_attempts=1,
            next_attempt_at=past,
            requested_by=cls.editor,
        )
        cls.not_yet_due = models.TurnitinSubmission.objects.create(
            article=cls.article,
            file=cls.file,
            status="RETRYING",
            send_attempts=1,
            next_attempt_at=timezone.now() + timedelta(hours=1),
            requested_by=cls.editor,
        )
        cls.report_failed = models.TurnitinSubmission.objects.create(
            article=cls.article,
            file=cls.second_file,
            tii_submission_id="tii-report-failed",
            status="COMPLETE",
            report_status="ERROR",
            requested_by=cls.editor,
        )
        models.TurnitinSubmission.objects.filter(pk=cls.report_failed.pk).update(
            date_created=timezone.now() - timedelta(hours=1),
        )

    def test_transient_send_failure_is_queued_for_retry(self):
        fake_client = mock.Mock()
        fake_client.create_submission.side_effect = outage()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            submission = logic.send_file_for_report(
                self.article,
                self.file,
                self.editor,
            )
        submission.refresh_from_db()
        self.assertEqual(submission.status, "RETRYING")
        self.assertEqual(submission.send_attempts, 1)
        delay = submission.next_attempt_at - timezone.now()
        self.assertAlmostEqual(
            delay.total_seconds(),
            tca_settings.RETRY_BASE_MINUTES * 60,
            delta=60,
        )

    def test_eula_refusal_raises_and_leaves_no_row(self):
        fake_client = mock.Mock()
        fake_client.create_submission.side_effect = eula_refusal()
        before = models.TurnitinSubmission.objects.count()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            with self.assertRaises(client.TurnitinError):
                logic.send_file_for_report(self.article, self.file, self.editor)
        self.assertEqual(models.TurnitinSubmission.objects.count(), before)

    def test_retry_send_repeats_only_the_upload_when_created(self):
        fake_client = mock.Mock()
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            logic.retry_send(self.upload_pending)
        fake_client.create_submission.assert_not_called()
        fake_client.upload_file.assert_called_once()
        self.upload_pending.refresh_from_db()
        self.assertEqual(self.upload_pending.status, "PROCESSING")
        self.assertEqual(self.upload_pending.send_attempts, 2)
        self.assertIsNone(self.upload_pending.next_attempt_at)

    def test_retry_send_creates_and_uploads_when_not_created(self):
        fake_client = mock.Mock()
        fake_client.create_submission.return_value = {"id": "tii-created"}
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            logic.retry_send(self.create_pending)
        fake_client.create_submission.assert_called_once()
        self.create_pending.refresh_from_db()
        self.assertEqual(self.create_pending.tii_submission_id, "tii-created")
        self.assertEqual(self.create_pending.status, "PROCESSING")

    def test_retry_send_gives_up_after_max_attempts(self):
        models.TurnitinSubmission.objects.filter(pk=self.create_pending.pk).update(
            send_attempts=tca_settings.MAX_SEND_ATTEMPTS - 1,
        )
        self.create_pending.refresh_from_db()
        fake_client = mock.Mock()
        fake_client.create_submission.side_effect = outage()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.retry_send(self.create_pending)
        self.create_pending.refresh_from_db()
        self.assertEqual(self.create_pending.status, "ERROR")
        self.assertIn("Gave up", self.create_pending.error_message)

    def test_retry_send_eula_refusal_marks_failed(self):
        fake_client = mock.Mock()
        fake_client.create_submission.side_effect = eula_refusal()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.retry_send(self.create_pending)
        self.create_pending.refresh_from_db()
        self.assertEqual(self.create_pending.status, "ERROR")
        self.assertIn("EULA", self.create_pending.error_message)

    def test_poll_command_retries_due_sends_only(self):
        fake_client = mock.Mock()
        fake_client.create_submission.return_value = {"id": "tii-polled"}
        fake_client.submission_info.return_value = {"status": "COMPLETE"}
        fake_client.similarity_report_info.return_value = {"status": "PROCESSING"}
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            call_command("poll_turnitin_tca")
        self.upload_pending.refresh_from_db()
        self.create_pending.refresh_from_db()
        self.not_yet_due.refresh_from_db()
        self.assertEqual(self.upload_pending.status, "PROCESSING")
        self.assertEqual(self.create_pending.status, "PROCESSING")
        self.assertEqual(self.not_yet_due.status, "RETRYING")
        self.assertEqual(self.not_yet_due.send_attempts, 1)

    def test_poll_command_retries_failed_report_request(self):
        fake_client = mock.Mock()
        fake_client.create_submission.return_value = {"id": "tii-polled"}
        fake_client.submission_info.return_value = {"status": "COMPLETE"}
        fake_client.similarity_report_info.return_value = {"status": "PROCESSING"}
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            call_command("poll_turnitin_tca")
        self.report_failed.refresh_from_db()
        self.assertTrue(self.report_failed.report_requested)
        self.assertEqual(self.report_failed.report_status, "PROCESSING")
        requested = [
            c.args[0] for c in fake_client.generate_similarity_report.call_args_list
        ]
        self.assertIn("tii-report-failed", requested)

    def test_index_setting_is_sent_on_the_report_request(self):
        configure_journal(self.journal_one, turnitin_index_submissions=True)
        fake_client = mock.Mock()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.request_similarity_report(self.report_failed)
        settings_sent = fake_client.generate_similarity_report.call_args.args[1]
        self.assertEqual(settings_sent["indexing_settings"], {"add_to_index": True})
        payload = logic.build_submission_payload(
            self.article,
            self.file,
            self.editor,
        )
        self.assertNotIn("add_to_index", payload["metadata"])
        self.report_failed.refresh_from_db()
        self.assertEqual(self.report_failed.report_attempts, 1)

    def test_index_setting_off_sends_false(self):
        configure_journal(self.journal_one, turnitin_index_submissions=False)
        fake_client = mock.Mock()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            logic.request_similarity_report(self.report_failed)
        settings_sent = fake_client.generate_similarity_report.call_args.args[1]
        self.assertEqual(settings_sent["indexing_settings"], {"add_to_index": False})

    def test_poll_command_stops_retrying_report_after_max_attempts(self):
        models.TurnitinSubmission.objects.filter(pk=self.report_failed.pk).update(
            report_attempts=tca_settings.MAX_REPORT_ATTEMPTS,
        )
        fake_client = mock.Mock()
        fake_client.create_submission.return_value = {"id": "tii-polled"}
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            call_command("poll_turnitin_tca")
        requested = [
            c.args[0] for c in fake_client.generate_similarity_report.call_args_list
        ]
        self.assertNotIn("tii-report-failed", requested)
        polled = [c.args[0] for c in fake_client.submission_info.call_args_list]
        self.assertNotIn("tii-report-failed", polled)

    def test_payload_sends_group_and_group_context(self):
        payload = logic.build_submission_payload(
            self.article,
            self.file,
            self.editor,
        )
        self.assertEqual(
            payload["metadata"]["group"],
            {
                "id": "janeway-article-{}".format(self.article.pk),
                "name": self.article.title,
                "type": "FOLDER",
            },
        )
        self.assertEqual(
            payload["metadata"]["group_context"],
            {
                "id": "janeway-journal-{}".format(self.journal_one.pk),
                "name": self.journal_one.name,
            },
        )
        self.assertEqual(
            payload["submitter"],
            logic.account_external_id(self.editor),
        )

    def test_integration_headers_come_from_plugin(self):
        turnitin = logic.get_client(self.journal_one)
        headers = turnitin.headers()
        self.assertEqual(
            headers["X-Turnitin-Integration-Name"],
            tca_settings.INTEGRATION_NAME,
        )
        self.assertEqual(
            headers["X-Turnitin-Integration-Version"],
            tca_settings.VERSION,
        )


@override_settings(URL_CONFIG="domain")
class TurnitinResendCommandTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        helpers.create_press()
        cls.journal_one, cls.journal_two = helpers.create_journals()
        helpers.create_roles(["editor", "author"])
        tca_settings.install()
        cls.editor = helpers.create_editor(cls.journal_one)
        cls.article = helpers.create_article(cls.journal_one)
        cls.failed_file = add_manuscript_file(cls.article, label="Failed")
        cls.recovered_file = add_manuscript_file(cls.article, label="Recovered")
        configure_journal(cls.journal_one)
        cls.failed = models.TurnitinSubmission.objects.create(
            article=cls.article,
            file=cls.failed_file,
            status="ERROR",
            requested_by=cls.editor,
        )
        models.TurnitinSubmission.objects.create(
            article=cls.article,
            file=cls.recovered_file,
            status="ERROR",
            requested_by=cls.editor,
            date_created=timezone.now() - timedelta(hours=2),
        )
        models.TurnitinSubmission.objects.create(
            article=cls.article,
            file=cls.recovered_file,
            tii_submission_id="tii-recovered",
            status="PROCESSING",
            requested_by=cls.editor,
        )

    def test_resends_only_files_whose_latest_send_failed(self):
        fake_client = mock.Mock()
        fake_client.create_submission.return_value = {"id": "tii-resent"}
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            call_command(
                "resend_failed_turnitin_tca",
                journal=self.journal_one.code,
                stdout=mock.Mock(),
            )
        fake_client.create_submission.assert_called_once()
        resent = models.TurnitinSubmission.objects.get(tii_submission_id="tii-resent")
        self.assertEqual(resent.file, self.failed_file)
        self.assertEqual(resent.requested_by, self.editor)
        self.assertEqual(resent.status, "PROCESSING")
        self.failed.refresh_from_db()
        self.assertEqual(self.failed.status, "ERROR")


@override_settings(URL_CONFIG="domain")
class TurnitinResendViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        helpers.create_press()
        cls.journal_one, cls.journal_two = helpers.create_journals()
        helpers.create_roles(["editor", "author"])
        tca_settings.install()
        clear_url_caches()
        importlib.reload(importlib.import_module(settings.ROOT_URLCONF))
        importlib.reload(importlib.import_module("core.include_urls"))
        cls.editor = helpers.create_editor(cls.journal_one)
        configure_journal(cls.journal_one)

        cls.article = helpers.create_article(cls.journal_one)
        cls.file = add_manuscript_file(cls.article)

        cls.failed_article = helpers.create_article(cls.journal_one)
        cls.failed_file = add_manuscript_file(cls.failed_article, label="Failed")
        cls.ok_file = add_manuscript_file(cls.failed_article, label="Processing")
        cls.report_failed_file = add_manuscript_file(
            cls.failed_article,
            label="Report failed",
        )
        cls.complete_file = add_manuscript_file(cls.failed_article, label="Done")
        models.TurnitinSubmission.objects.create(
            article=cls.failed_article,
            file=cls.failed_file,
            status="ERROR",
            error_message="UNSUPPORTED_FILETYPE",
            requested_by=cls.editor,
        )
        models.TurnitinSubmission.objects.create(
            article=cls.failed_article,
            file=cls.ok_file,
            tii_submission_id="tii-ok",
            status="PROCESSING",
            requested_by=cls.editor,
        )
        models.TurnitinSubmission.objects.create(
            article=cls.failed_article,
            file=cls.report_failed_file,
            tii_submission_id="tii-report-failed",
            status="COMPLETE",
            report_status="ERROR",
            requested_by=cls.editor,
        )
        cls.complete = models.TurnitinSubmission.objects.create(
            article=cls.failed_article,
            file=cls.complete_file,
            tii_submission_id="tii-complete",
            status="COMPLETE",
            report_status="COMPLETE",
            similarity_score=4,
            requested_by=cls.editor,
        )

    def test_eula_refusal_sends_editor_to_eula_then_resends(self):
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        fake_client.create_submission.side_effect = [
            eula_refusal(),
            {"id": "tii-after-eula"},
        ]
        fake_client.eula_version_info.return_value = {"version": "v2"}
        fake_client.eula_page.return_value = "<p>EULA</p>"
        send_url = reverse(
            "turnitin_tca_send_file",
            kwargs={"article_id": self.article.pk, "file_id": self.file.pk},
        )
        eula_url = reverse("turnitin_tca_eula", kwargs={"article_id": self.article.pk})
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            refused = self.client.post(send_url, SERVER_NAME=self.journal_one.domain)
            self.assertRedirects(refused, eula_url, fetch_redirect_response=False)
            self.assertFalse(
                models.TurnitinSubmission.objects.filter(file=self.file).exists(),
            )
            accepted = self.client.post(eula_url, SERVER_NAME=self.journal_one.domain)

        self.assertRedirects(
            accepted,
            reverse("turnitin_tca_article", kwargs={"article_id": self.article.pk}),
            fetch_redirect_response=False,
        )
        fake_client.accept_eula.assert_called_once()
        payload = fake_client.create_submission.call_args_list[1].args[0]
        self.assertEqual(payload["eula"]["version"], "v2")
        submission = models.TurnitinSubmission.objects.get(file=self.file)
        self.assertEqual(submission.tii_submission_id, "tii-after-eula")

    def test_article_page_offers_resend_for_failures(self):
        self.client.force_login(self.editor)
        response = self.client.get(
            reverse(
                "turnitin_tca_article",
                kwargs={"article_id": self.failed_article.pk},
            ),
            SERVER_NAME=self.journal_one.domain,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Turnitin cannot check this type of file.")
        self.assertNotContains(response, "Turnitin said:")
        self.assertContains(
            response,
            reverse(
                "turnitin_tca_send_file",
                kwargs={
                    "article_id": self.failed_article.pk,
                    "file_id": self.failed_file.pk,
                },
            ),
        )
        self.assertContains(response, "Request report again")
        self.assertContains(response, "Resend all failed (1)")

    def test_resend_failed_asks_for_confirmation_first(self):
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            response = self.client.post(
                reverse(
                    "turnitin_tca_resend_failed",
                    kwargs={"article_id": self.failed_article.pk},
                ),
                data={forms.ResendFailedForm.CONFIRMABLE_BUTTON_NAME: "1"},
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "confirm_modal")
        fake_client.create_submission.assert_not_called()

    def test_resend_failed_resends_only_failed_files(self):
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        fake_client.create_submission.return_value = {"id": "tii-bulk"}
        with (
            mock.patch.object(logic, "get_client", return_value=fake_client),
            mock.patch("core.models.File.get_file", return_value=b"bytes"),
        ):
            response = self.client.post(
                reverse(
                    "turnitin_tca_resend_failed",
                    kwargs={"article_id": self.failed_article.pk},
                ),
                data={forms.ResendFailedForm.CONFIRMED_BUTTON_NAME: "1"},
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertEqual(response.status_code, 302)
        fake_client.create_submission.assert_called_once()
        latest = models.TurnitinSubmission.objects.filter(
            file=self.failed_file,
        ).first()
        self.assertEqual(latest.tii_submission_id, "tii-bulk")
        self.assertEqual(latest.status, "PROCESSING")

    def test_view_report_eula_refusal_sends_editor_to_eula(self):
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        fake_client.viewer_url.side_effect = eula_refusal()
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            response = self.client.get(
                reverse(
                    "turnitin_tca_view_report",
                    kwargs={
                        "article_id": self.failed_article.pk,
                        "submission_id": self.complete.pk,
                    },
                ),
                SERVER_NAME=self.journal_one.domain,
            )
        self.assertRedirects(
            response,
            reverse(
                "turnitin_tca_eula",
                kwargs={"article_id": self.failed_article.pk},
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self.client.session["turnitin_pending_report"],
            self.complete.pk,
        )

    def test_article_page_shows_raw_error_to_staff(self):
        staff = helpers.create_editor(
            self.journal_one,
            email="staff-editor@example.com",
            is_staff=True,
        )
        self.client.force_login(staff)
        response = self.client.get(
            reverse(
                "turnitin_tca_article",
                kwargs={"article_id": self.failed_article.pk},
            ),
            SERVER_NAME=self.journal_one.domain,
        )
        self.assertContains(response, "Turnitin said: UNSUPPORTED_FILETYPE")

    def test_view_report_failure_shows_friendly_message(self):
        self.client.force_login(self.editor)
        fake_client = mock.Mock()
        fake_client.viewer_url.side_effect = client.TurnitinError(
            "TCA POST viewer-url returned 500",
            status_code=500,
            body="raw api detail",
        )
        with mock.patch.object(logic, "get_client", return_value=fake_client):
            response = self.client.get(
                reverse(
                    "turnitin_tca_view_report",
                    kwargs={
                        "article_id": self.failed_article.pk,
                        "submission_id": self.complete.pk,
                    },
                ),
                SERVER_NAME=self.journal_one.domain,
                follow=True,
            )
        self.assertContains(
            response,
            "Viewer not currently available, please try again later.",
        )
        self.assertNotContains(response, "raw api detail")
