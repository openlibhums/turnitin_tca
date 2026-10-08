from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms.forms import ConfirmableForm
from plugins.turnitin_tca import plugin_settings


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


class ReportOptionsForm(forms.Form):
    """The exclusion options sent when a similarity report is generated."""

    turnitin_exclude_quotes = forms.BooleanField(
        label="Exclude quotes",
        required=False,
        help_text="Leave text in quotation marks out of the similarity score.",
    )
    turnitin_exclude_bibliography = forms.BooleanField(
        label="Exclude bibliography",
        required=False,
        help_text="Leave the reference list out of the similarity score.",
    )
    turnitin_exclude_citations = forms.BooleanField(
        label="Exclude citations",
        required=False,
        help_text="Leave in-text citations out of the similarity score.",
    )
    turnitin_exclude_abstract = forms.BooleanField(
        label="Exclude abstract",
        required=False,
        help_text="Leave the abstract out of the similarity score.",
    )
    turnitin_exclude_methods = forms.BooleanField(
        label="Exclude methods",
        required=False,
        help_text="Leave the methods section out of the similarity score.",
    )
    turnitin_exclude_custom_sections = forms.BooleanField(
        label="Exclude custom sections",
        required=False,
        help_text="Leave out the custom sections defined in your iThenticate account.",
    )
    turnitin_exclude_preprints = forms.BooleanField(
        label="Exclude preprints",
        required=False,
        help_text="Leave matches against preprints out of the similarity score.",
    )
    turnitin_exclude_small_matches = forms.BooleanField(
        label="Exclude small matches",
        required=False,
        help_text="Leave matches shorter than the word count below out of the similarity score.",
    )
    turnitin_small_match_words = forms.IntegerField(
        label="Small match word count",
        required=False,
        min_value=plugin_settings.MIN_SMALL_MATCH_WORDS,
        initial=plugin_settings.MIN_SMALL_MATCH_WORDS,
        help_text="At least {}.".format(plugin_settings.MIN_SMALL_MATCH_WORDS),
    )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("turnitin_exclude_small_matches") and not cleaned_data.get(
            "turnitin_small_match_words"
        ):
            self.add_error(
                "turnitin_small_match_words",
                "Enter the word count for excluding small matches.",
            )
        if not cleaned_data.get("turnitin_small_match_words"):
            cleaned_data["turnitin_small_match_words"] = (
                plugin_settings.MIN_SMALL_MATCH_WORDS
            )
        return cleaned_data


class TurnitinJournalForm(ReportOptionsForm):
    turnitin_enabled = forms.BooleanField(
        label="Turnitin Core API enabled",
        required=False,
        help_text="Turn the integration on for this journal.",
    )


class TurnitinAdminForm(ReportOptionsForm):
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
