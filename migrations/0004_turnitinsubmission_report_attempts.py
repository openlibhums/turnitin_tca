from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("turnitin_tca", "0003_turnitinsubmission_send_retry"),
    ]

    operations = [
        migrations.AddField(
            model_name="turnitinsubmission",
            name="report_attempts",
            field=models.PositiveIntegerField(
                default=0,
                help_text="How many times Generate Similarity Report has been "
                "requested for this row.",
            ),
        ),
    ]
