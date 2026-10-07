from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from plugins.turnitin_tca import logic, models
from plugins.turnitin_tca import plugin_settings as tca_settings
from plugins.turnitin_tca.client import TurnitinError
from utils.logger import get_logger

logger = get_logger(__name__)

# Begin polling only once a webhook has had a fair chance to arrive.
POLL_AFTER = timedelta(minutes=30)

# Cap the work per run so a large backlog cannot make a single invocation run
# unbounded; the remainder is picked up on the next scheduled run.
MAX_BATCH = 200

# A crude cross-process lock so two overlapping cron runs (a slow run not yet
# finished when the next fires) do not poll and save the same rows at once.
LOCK_KEY = "turnitin_tca_poll_lock"
LOCK_TIMEOUT = 30 * 60


class Command(BaseCommand):
    """Polling fallback for Turnitin submissions that have not had a webhook.

    Turnitin recommends polling Get Submission Info / Get Similarity Report Info
    when no webhook has arrived within 30 minutes, and retrying failed requests.
    Run this on a schedule (e.g. every 10 minutes) via cron.
    """

    help = "Poll Turnitin for submissions still awaiting a result."

    def handle(self, *args, **options):
        # cache.add only succeeds if the key is absent, giving us a mutex.
        if not cache.add(LOCK_KEY, "1", LOCK_TIMEOUT):
            self.stdout.write("Another poll run is in progress; skipping.")
            return
        try:
            self.poll(*args, **options)
        finally:
            cache.delete(LOCK_KEY)

    def poll(self, *args, **options):
        self.retry_sends()

        cutoff = timezone.now() - POLL_AFTER
        pending = (
            models.TurnitinSubmission.objects.filter(
                tii_submission_id__isnull=False,
                date_created__lte=cutoff,
            )
            .exclude(
                report_status="COMPLETE",
            )
            .exclude(
                status__in=["ERROR", "RETRYING"],
            )
            .exclude(
                report_status="ERROR",
                report_attempts__gte=tca_settings.MAX_REPORT_ATTEMPTS,
            )[:MAX_BATCH]
        )

        self.stdout.write("Polling {} Turnitin submission(s).".format(len(pending)))
        for submission in pending:
            try:
                logic.poll_submission(submission)
            except TurnitinError as exc:
                logger.warning("Poll failed for %s: %s", submission.pk, exc)

    def retry_sends(self):
        """Retry sends that failed on a transient error and are now due."""
        due = models.TurnitinSubmission.objects.filter(
            status="RETRYING",
            next_attempt_at__lte=timezone.now(),
        ).select_related("article__journal", "file", "requested_by")[:MAX_BATCH]

        self.stdout.write("Retrying {} Turnitin send(s).".format(len(due)))
        for submission in due:
            logic.retry_send(submission)
