from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("turnitin_tca", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="turnitinsubmission",
            name="report_notified",
            field=models.BooleanField(
                default=False,
                help_text="Whether the requesting editor has been emailed that "
                "the report is complete. Guards against duplicate emails when a "
                "webhook and the polling fallback both observe completion.",
            ),
        ),
    ]
