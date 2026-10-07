from django.contrib import admin

from plugins.turnitin_tca import models


class TurnitinSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "pk",
        "article",
        "file_label",
        "status",
        "report_status",
        "similarity_score",
        "requested_by",
        "date_created",
    )
    list_filter = ("status", "report_status")
    search_fields = ("tii_submission_id", "file_label", "article__title")
    raw_id_fields = ("article", "file", "requested_by")


class TurnitinEulaAcceptanceAdmin(admin.ModelAdmin):
    list_display = ("account", "version", "accepted_timestamp", "language")
    search_fields = ("account__email", "version")
    raw_id_fields = ("account",)


admin.site.register(models.TurnitinSubmission, TurnitinSubmissionAdmin)
admin.site.register(models.TurnitinEulaAcceptance, TurnitinEulaAcceptanceAdmin)
