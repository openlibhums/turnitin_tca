from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from journal import models as journal_models
from plugins.turnitin_tca import logic, models


class Command(BaseCommand):
    """Resend every file whose most recent send to Turnitin failed.

    Intended for recovering after an outage. Each file is sent as a new
    submission on behalf of the editor who originally sent it; transient
    failures are then retried by poll_turnitin_tca as usual.
    """

    help = "Resend files whose most recent Turnitin send failed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--journal",
            help="Only resend for the journal with this code.",
        )
        parser.add_argument(
            "--since",
            help="Only resend submissions created on or after this date "
            "(YYYY-MM-DD).",
        )

    def handle(self, *args, **options):
        submissions = models.TurnitinSubmission.objects.select_related(
            "article__journal",
            "file",
            "requested_by",
        )
        if options["journal"]:
            try:
                journal = journal_models.Journal.objects.get(code=options["journal"])
            except journal_models.Journal.DoesNotExist:
                raise CommandError("No journal with code {}".format(options["journal"]))
            submissions = submissions.filter(article__journal=journal)
        if options["since"]:
            try:
                since = datetime.strptime(options["since"], "%Y-%m-%d")
            except ValueError:
                raise CommandError("--since must be a date in YYYY-MM-DD form.")
            submissions = submissions.filter(
                date_created__gte=timezone.make_aware(since),
            )

        failed = logic.latest_failed_submissions(submissions)
        self.stdout.write("Resending {} failed submission(s).".format(len(failed)))
        for submission in failed:
            if not logic.is_enabled(submission.article.journal):
                continue
            fresh = logic.resend_submission(submission)
            self.stdout.write(
                "{} -> {} ({})".format(submission.pk, fresh.pk, fresh.status),
            )
