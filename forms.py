from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms.forms import ConfirmableForm


class RegenerateReportForm(ConfirmableForm):
    """Confirm before re-running a similarity report on an existing file."""

    QUESTION = _(
        "Request a new similarity report for this file? This re-runs the "
        "check against the same uploaded file and overwrites the current "
        "similarity score."
    )


class ResendFailedForm(ConfirmableForm):
    """Confirm before resending every file whose last send failed."""

    QUESTION = _(
        "Resend every file whose last send to Turnitin failed? Each file is "
        "sent as a new submission."
    )


class DeleteWebhookForm(ConfirmableForm):
    """Confirm before deleting a single registered webhook."""

    QUESTION = _(
        "Delete this webhook? Turnitin will stop sending callbacks to its URL."
    )


class DeleteAllWebhooksForm(ConfirmableForm):
    """Confirm before deleting every registered webhook."""

    QUESTION = _(
        "Delete all registered webhooks? Turnitin will stop sending callbacks "
        "until a webhook is registered again."
    )


class TurnitinJournalForm(forms.Form):
    turnitin_enabled = forms.BooleanField(
        label="Turnitin Core API enabled",
        required=False,
        help_text="Turn the integration on for this journal.",
    )


class TurnitinAdminForm(forms.Form):
    """The install-wide Turnitin account settings, shared by every journal."""

    turnitin_api_url = forms.CharField(
        label="API URL",
        required=False,
        help_text="e.g. https://your-tenant.turnitin.com",
    )
    turnitin_api_key = forms.CharField(
        label="API Key",
        required=False,
        widget=forms.PasswordInput(render_value=True),
    )
    turnitin_index_submissions = forms.BooleanField(
        label="Add submissions to your iThenticate repository for future comparison",
        required=False,
    )
    turnitin_save_report_changes = forms.BooleanField(
        label="Save iThenticate report change",
        required=False,
        help_text=(
            "Keep changes made in the report viewer (such as excluded "
            "sources) for the next time the report is opened. If the "
            "similarity score changes, Janeway is told the new score."
        ),
    )
    turnitin_webhook_url = forms.URLField(
        label="Webhook URL Override",
        required=False,
        help_text=(
            "Leave blank to use this install's webhook URL. Set to a tunnel "
            "URL (e.g. ngrok) to receive callbacks during development."
        ),
    )
