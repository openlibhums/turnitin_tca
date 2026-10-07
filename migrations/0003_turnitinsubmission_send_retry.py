from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("turnitin_tca", "0002_turnitinsubmission_report_notified"),
    ]

    operations = [
        migrations.AddField(
            model_name="turnitinsubmission",
            name="send_attempts",
            field=models.PositiveIntegerField(
                default=0,
                help_text="How many times Create Submission / Upload File has "
                "been attempted for this row.",
            ),
        ),
        migrations.AddField(
            model_name="turnitinsubmission",
            name="next_attempt_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="When a RETRYING send is next due to be attempted.",
            ),
        ),
        migrations.AlterField(
            model_name="turnitinsubmission",
            name="status",
            field=models.CharField(
                choices=[
                    ("CREATED", "Created"),
                    ("RETRYING", "Retrying"),
                    ("PROCESSING", "Processing"),
                    ("COMPLETE", "Complete"),
                    ("ERROR", "Error"),
                ],
                default="CREATED",
                max_length=20,
            ),
        ),
    ]
