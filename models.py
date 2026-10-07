from django.db import models
from django.utils import timezone

# Plain-language explanations of the error_code Turnitin reports when it
# cannot process a submitted file.
ERROR_CODE_MESSAGES = {
    "UNSUPPORTED_FILETYPE": "Turnitin cannot check this type of file.",
    "PROCESSING_ERROR": "Turnitin could not process this file.",
    "CANNOT_EXTRACT_TEXT": "Turnitin could not read any text in this file.",
    "TOO_LITTLE_TEXT": "This file does not contain enough text to check.",
    "TOO_MUCH_TEXT": "This file contains more text than Turnitin can check.",
    "TOO_MANY_PAGES": "This file has more pages than Turnitin can check.",
    "FILE_LOCKED": "This file is locked or password protected, so Turnitin "
    "cannot read it.",
    "CORRUPT_FILE": "This file appears to be damaged and cannot be read.",
}

# Error messages Janeway writes itself, which are already safe to show editors.
EULA_RESEND_MESSAGE = (
    "The sending editor must accept the Turnitin EULA; resend the file from "
    "the article page."
)
MISSING_FILE_MESSAGE = "The file or the editor who sent it no longer exists."
PLAIN_ERROR_MESSAGES = (EULA_RESEND_MESSAGE, MISSING_FILE_MESSAGE)


class TurnitinSubmission(models.Model):
    """A single core.File that has been sent to Turnitin Core API."""

    SUBMISSION_STATUS_CHOICES = (
        ("CREATED", "Created"),
        ("RETRYING", "Retrying"),
        ("PROCESSING", "Processing"),
        ("COMPLETE", "Complete"),
        ("ERROR", "Error"),
    )
    REPORT_STATUS_CHOICES = (
        ("", "Not requested"),
        ("PENDING", "Pending"),
        ("PROCESSING", "Processing"),
        ("COMPLETE", "Complete"),
        ("ERROR", "Error"),
    )

    article = models.ForeignKey(
        "submission.Article",
        on_delete=models.CASCADE,
        related_name="turnitin_submissions",
    )
    file = models.ForeignKey(
        "core.File",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="turnitin_submissions",
    )
    file_label = models.CharField(
        max_length=1000,
        blank=True,
        help_text="Snapshot of the file label at the time it was sent.",
    )
    tii_submission_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=SUBMISSION_STATUS_CHOICES,
        default="CREATED",
    )
    report_requested = models.BooleanField(default=False)
    report_attempts = models.PositiveIntegerField(
        default=0,
        help_text="How many times Generate Similarity Report has been "
        "requested for this row.",
    )
    report_status = models.CharField(
        max_length=20,
        choices=REPORT_STATUS_CHOICES,
        blank=True,
        default="",
    )
    similarity_score = models.IntegerField(blank=True, null=True)
    report_notified = models.BooleanField(
        default=False,
        help_text="Whether the requesting editor has been emailed that the "
        "report is complete. Guards against duplicate emails when a webhook "
        "and the polling fallback both observe completion.",
    )
    error_message = models.TextField(blank=True, null=True)
    send_attempts = models.PositiveIntegerField(
        default=0,
        help_text="How many times Create Submission / Upload File has been "
        "attempted for this row.",
    )
    next_attempt_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When a RETRYING send is next due to be attempted.",
    )
    requested_by = models.ForeignKey(
        "core.Account",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    last_webhook = models.DateTimeField(blank=True, null=True)
    date_created = models.DateTimeField(default=timezone.now)
    last_modified = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-date_created",)

    def __str__(self):
        return "Turnitin submission {} for file {}".format(
            self.tii_submission_id or "(pending)",
            self.file_id,
        )

    @property
    def editor_error(self):
        """A user-friendly explanation of why this row failed.

        error_message keeps the raw detail (often Turnitin's API response) for
        Janeway admins to raise support tickets with; editors see this instead.
        """
        if self.error_message in ERROR_CODE_MESSAGES:
            return ERROR_CODE_MESSAGES[self.error_message]
        if self.error_message in PLAIN_ERROR_MESSAGES:
            return self.error_message
        if self.status != "ERROR" and self.report_status == "ERROR":
            return (
                "The similarity report could not be generated. Please try again later."
            )
        return "The file could not be sent to Turnitin. Please try again later."

    @property
    def is_complete(self):
        return self.report_status == "COMPLETE"

    @property
    def can_view_report(self):
        return self.is_complete and bool(self.tii_submission_id)


class TurnitinEulaAcceptance(models.Model):
    """Records a user's acceptance of a Turnitin EULA version."""

    account = models.ForeignKey(
        "core.Account",
        on_delete=models.CASCADE,
        related_name="turnitin_eula_acceptances",
    )
    version = models.CharField(max_length=100)
    accepted_timestamp = models.DateTimeField(default=timezone.now)
    language = models.CharField(max_length=20, default="en-US")

    class Meta:
        unique_together = ("account", "version")

    def __str__(self):
        return "{} accepted EULA {}".format(self.account, self.version)
