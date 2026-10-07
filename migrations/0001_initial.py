from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "__first__"),
        ("submission", "__first__"),
    ]

    operations = [
        migrations.CreateModel(
            name="TurnitinEulaAcceptance",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("version", models.CharField(max_length=100)),
                (
                    "accepted_timestamp",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("language", models.CharField(default="en-US", max_length=20)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="turnitin_eula_acceptances",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "unique_together": {("account", "version")},
            },
        ),
        migrations.CreateModel(
            name="TurnitinSubmission",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "file_label",
                    models.CharField(
                        blank=True,
                        help_text="Snapshot of the file label at the time it was sent.",
                        max_length=1000,
                    ),
                ),
                (
                    "tii_submission_id",
                    models.CharField(
                        blank=True, db_index=True, max_length=100, null=True
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("CREATED", "Created"),
                            ("PROCESSING", "Processing"),
                            ("COMPLETE", "Complete"),
                            ("ERROR", "Error"),
                        ],
                        default="CREATED",
                        max_length=20,
                    ),
                ),
                ("report_requested", models.BooleanField(default=False)),
                (
                    "report_status",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("", "Not requested"),
                            ("PENDING", "Pending"),
                            ("PROCESSING", "Processing"),
                            ("COMPLETE", "Complete"),
                            ("ERROR", "Error"),
                        ],
                        default="",
                        max_length=20,
                    ),
                ),
                ("similarity_score", models.IntegerField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True, null=True)),
                (
                    "last_webhook",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "date_created",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("last_modified", models.DateTimeField(auto_now=True)),
                (
                    "article",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="turnitin_submissions",
                        to="submission.article",
                    ),
                ),
                (
                    "file",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="turnitin_submissions",
                        to="core.file",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-date_created",),
            },
        ),
    ]
