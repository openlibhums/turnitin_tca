import json

from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render, redirect, reverse, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from plugins.turnitin_tca import forms, logic, models, plugin_settings
from plugins.turnitin_tca.client import TurnitinError
from security.decorators import editor_user_required, has_journal
from submission import models as submission_models
from utils import setting_handler
from utils.logger import get_logger

logger = get_logger(__name__)


@has_journal
@editor_user_required
def manager(request):
    plugin = logic.get_plugin()
    initial = {
        name: setting_handler.get_plugin_setting(
            plugin,
            name,
            request.journal,
            create=True,
        ).processed_value
        for name in plugin_settings.SETTING_NAMES
    }
    admin_form = forms.TurnitinAdminForm(initial=initial)

    if request.POST:
        admin_form = forms.TurnitinAdminForm(request.POST)
        if admin_form.is_valid():
            for name, value in admin_form.cleaned_data.items():
                setting_handler.save_plugin_setting(
                    plugin,
                    name,
                    value,
                    request.journal,
                )
            messages.add_message(
                request,
                messages.SUCCESS,
                "Turnitin settings updated.",
            )
            # The first call with new credentials is Get Features Enabled,
            # which also tells us whether the tenant requires the EULA.
            if logic.is_enabled(request.journal):
                check_connection(request)
            else:
                # The credentials may now belong to another tenant, so ask
                # again once the integration is configured.
                logic.save_setting(
                    plugin_settings.REQUIRE_EULA_SETTING,
                    "",
                    request.journal,
                )
            return redirect(reverse("turnitin_tca_manager"))

    webhook_url = logic.get_setting(
        "turnitin_webhook_url",
        request.journal,
    ) or request.journal.site_url(path=reverse("turnitin_tca_webhook"))
    template = "turnitin_tca/manager.html"
    context = {
        "admin_form": admin_form,
        "webhook_url": webhook_url,
        "webhook_id": logic.get_setting("turnitin_webhook_id", request.journal),
        "require_eula": logic.get_setting(
            plugin_settings.REQUIRE_EULA_SETTING,
            request.journal,
        ),
    }
    return render(request, template, context)


def check_connection(request):
    """Call Get Features Enabled and tell the editor how it went."""
    try:
        features = logic.refresh_features(request.journal)
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "Could not connect to Turnitin. Check the API URL and key, or "
            "try again later.",
            exc,
        )
        return
    product = features.get("product_name", "Turnitin")
    if logic.tenant_requires_eula(features):
        eula = "Editors will be asked to accept the Turnitin EULA before sending files."
    else:
        eula = "Your licence does not require editors to accept a EULA."
    messages.add_message(
        request,
        messages.SUCCESS,
        "Connection succeeded. Licensed product: {}. {}".format(product, eula),
    )


@has_journal
@editor_user_required
def test_connection(request):
    check_connection(request)
    return redirect(reverse("turnitin_tca_manager"))


@has_journal
@editor_user_required
@require_POST
def register_webhook(request):
    if not logic.is_enabled(request.journal):
        messages.add_message(
            request,
            messages.ERROR,
            "Set the API URL and key before registering the webhook.",
        )
        return redirect(reverse("turnitin_tca_manager"))

    try:
        result = logic.register_webhook(request.journal)
        messages.add_message(
            request,
            messages.SUCCESS,
            "Webhook registered with Turnitin (id {}). A signing secret was "
            "generated automatically.".format(result.get("id", "")),
        )
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "The webhook could not be registered. Please try again later.",
            exc,
        )
    return redirect(reverse("turnitin_tca_manager"))


def render_webhooks(request, confirm_modal=None, confirm_form_id=None):
    """Render the webhooks list, optionally with a delete confirmation modal."""
    hooks = []
    try:
        hooks = logic.list_webhooks(request.journal)
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "Could not load the registered webhooks. Please try again later.",
            exc,
        )
    template = "turnitin_tca/webhooks.html"
    context = {
        "webhooks": hooks,
        "registered_id": logic.get_setting("turnitin_webhook_id", request.journal),
        "confirmable_button_name": forms.DeleteWebhookForm.CONFIRMABLE_BUTTON_NAME,
        "confirm_modal": confirm_modal,
        "confirm_form_id": confirm_form_id,
    }
    return render(request, template, context)


@has_journal
@editor_user_required
def webhooks(request):
    return render_webhooks(request)


@has_journal
@editor_user_required
@require_POST
def delete_webhook(request, webhook_id):
    form = forms.DeleteWebhookForm(request.POST)
    if not (form.is_valid() and form.is_confirmed()):
        # First click: re-render with the confirmation modal for this row.
        return render_webhooks(
            request,
            confirm_modal=form.modal,
            confirm_form_id="del-{}".format(webhook_id),
        )
    try:
        logic.delete_webhook(request.journal, webhook_id)
        messages.add_message(request, messages.SUCCESS, "Webhook deleted.")
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "The webhook could not be deleted. Please try again later.",
            exc,
        )
    return redirect(reverse("turnitin_tca_webhooks"))


@has_journal
@editor_user_required
@require_POST
def delete_all_webhooks(request):
    form = forms.DeleteAllWebhooksForm(request.POST)
    if not (form.is_valid() and form.is_confirmed()):
        # First click: re-render with the confirmation modal.
        return render_webhooks(
            request,
            confirm_modal=form.modal,
            confirm_form_id="del-all",
        )
    try:
        count = logic.delete_all_webhooks(request.journal)
        messages.add_message(
            request,
            messages.SUCCESS,
            "Deleted {} webhook(s).".format(count),
        )
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "The webhooks could not be deleted. Please try again later.",
            exc,
        )
    return redirect(reverse("turnitin_tca_webhooks"))


@has_journal
@editor_user_required
def article_reports(request, article_id):
    article = get_object_or_404(
        submission_models.Article.objects.prefetch_related(
            "reviewround_set__review_files",
        ),
        pk=article_id,
        journal=request.journal,
    )
    template = "turnitin_tca/article.html"
    context = logic.article_page_context(article, request.journal)
    context["confirmable_button_name"] = forms.RegenerateReportForm.CONFIRMABLE_BUTTON_NAME
    return render(request, template, context)


@has_journal
@editor_user_required
@require_POST
def send_file(request, article_id, file_id):
    article = get_object_or_404(
        submission_models.Article.objects.prefetch_related(
            "reviewround_set__review_files",
        ),
        pk=article_id,
        journal=request.journal,
    )
    if not logic.is_enabled(request.journal):
        messages.add_message(
            request,
            messages.ERROR,
            "Turnitin is not configured for this journal.",
        )
        return redirect(reverse("turnitin_tca_article", args=[article.pk]))

    # Resolve the file from the gathered, send-eligible set to avoid arbitrary ids.
    eligible = {e["file"].pk: e["file"] for e in logic.get_article_files(article)}
    file_obj = eligible.get(int(file_id))
    if not file_obj:
        messages.add_message(
            request,
            messages.ERROR,
            "That file cannot be sent to Turnitin.",
        )
        return redirect(reverse("turnitin_tca_article", args=[article.pk]))

    try:
        eula_block, accepted = logic.required_eula_block(
            request.journal,
            request.user,
        )
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "The Turnitin EULA could not be loaded. Please try again later.",
            exc,
        )
        return redirect(reverse("turnitin_tca_article", args=[article.pk]))
    if not accepted:
        request.session["turnitin_pending_file"] = file_obj.pk
        return redirect(reverse("turnitin_tca_eula", args=[article.pk]))

    return redirect(
        logic.send_file_from_request(request, article, file_obj, eula_block),
    )


@has_journal
@editor_user_required
@require_POST
def resend_failed(request, article_id):
    article = get_object_or_404(
        submission_models.Article.objects.prefetch_related(
            "reviewround_set__review_files",
        ),
        pk=article_id,
        journal=request.journal,
    )
    article_url = reverse("turnitin_tca_article", args=[article.pk])
    if not logic.is_enabled(request.journal):
        messages.add_message(
            request,
            messages.ERROR,
            "Turnitin is not configured for this journal.",
        )
        return redirect(article_url)

    form = forms.ResendFailedForm(request.POST)
    if not (form.is_valid() and form.is_confirmed()):
        # First click: render the page with the confirmation modal.
        template = "turnitin_tca/article.html"
        context = logic.article_page_context(article, request.journal)
        context.update(
            {
                "confirmable_button_name": form.CONFIRMABLE_BUTTON_NAME,
                "confirm_modal": form.modal,
                "confirm_form_id": "resend-failed",
            }
        )
        return render(request, template, context)

    try:
        eula_block, accepted = logic.required_eula_block(
            request.journal,
            request.user,
        )
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "The Turnitin EULA could not be loaded. Please try again later.",
            exc,
        )
        return redirect(article_url)
    if not accepted:
        messages.add_message(
            request,
            messages.INFO,
            "Accept the Turnitin EULA, then resend the failed files.",
        )
        return redirect(reverse("turnitin_tca_eula", args=[article.pk]))

    rows = logic.failed_rows(logic.article_report_rows(article))
    sent, retrying, failed = 0, 0, []
    for row in rows:
        try:
            submission = logic.send_file_for_report(
                article,
                row["file"],
                request.user,
                eula_block,
            )
        except TurnitinError as exc:
            if exc.eula_required:
                messages.add_message(
                    request,
                    messages.INFO,
                    "Turnitin requires you to accept its End User Licence "
                    "Agreement before files can be sent.",
                )
                return redirect(reverse("turnitin_tca_eula", args=[article.pk]))
            failed.append((row["file"].original_filename, exc))
            continue
        if submission.status == "RETRYING":
            retrying += 1
        else:
            sent += 1

    summary = "Resent {} file(s).".format(sent)
    if retrying:
        summary += " {} will be retried automatically.".format(retrying)
    messages.add_message(request, messages.SUCCESS, summary)
    for filename, exc in failed:
        logic.editor_error(
            request,
            "{} could not be sent to Turnitin. Please try again later.".format(
                filename,
            ),
            exc,
        )
    return redirect(article_url)


@has_journal
@editor_user_required
def eula(request, article_id):
    article = get_object_or_404(
        submission_models.Article,
        pk=article_id,
        journal=request.journal,
    )
    try:
        version_info = logic.get_latest_eula(request.journal)
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "The Turnitin EULA could not be loaded. Please try again later.",
            exc,
        )
        return redirect(reverse("turnitin_tca_article", args=[article.pk]))

    version = version_info.get("version")
    try:
        eula_html = logic.get_client(
            request.journal,
            interactive=True,
        ).eula_page(version)
    except TurnitinError:
        eula_html = ""

    if request.method == "POST":
        article_url = reverse("turnitin_tca_article", args=[article.pk])
        pending_report = request.session.pop("turnitin_pending_report", None)
        pending_file = request.session.pop("turnitin_pending_file", None)
        try:
            logic.accept_eula(request.journal, request.user, version)
        except TurnitinError as exc:
            # Acceptance is recorded locally regardless, and a send carries it
            # in its eula block, so only a viewer launch depends on this call.
            logger.error("Could not record Turnitin EULA acceptance: %s", exc)
            if pending_report:
                logic.editor_error(
                    request,
                    "Your acceptance could not be recorded with Turnitin. "
                    "Please try again later.",
                    exc,
                )
                return redirect(article_url)

        if pending_report:
            return redirect(
                reverse("turnitin_tca_view_report", args=[article.pk, pending_report]),
            )
        eligible = {e["file"].pk: e["file"] for e in logic.get_article_files(article)}
        if pending_file in eligible:
            return redirect(
                logic.send_file_from_request(
                    request,
                    article,
                    eligible[pending_file],
                    logic.build_eula_block(request.user, version),
                ),
            )
        return redirect(article_url)

    template = "turnitin_tca/eula.html"
    context = {
        "article": article,
        "version": version,
        "eula_html": logic.sanitize_eula_html(eula_html),
        "eula_url": version_info.get("url"),
    }
    return render(request, template, context)


@has_journal
@editor_user_required
def view_report(request, article_id, submission_id):
    submission = get_object_or_404(
        models.TurnitinSubmission,
        pk=submission_id,
        article_id=article_id,
        article__journal=request.journal,
    )
    if not submission.can_view_report:
        messages.add_message(
            request,
            messages.WARNING,
            "The report is not ready yet.",
        )
        return redirect(
            reverse("turnitin_tca_article", args=[submission.article_id]),
        )
    try:
        url = logic.get_viewer_url(submission, request.user)
    except TurnitinError as exc:
        if exc.eula_required:
            request.session["turnitin_pending_report"] = submission.pk
            return redirect(
                reverse("turnitin_tca_eula", args=[submission.article_id]),
            )
        logic.editor_error(
            request,
            "Viewer not currently available, please try again later.",
            exc,
        )
        return redirect(
            reverse("turnitin_tca_article", args=[submission.article_id]),
        )
    return redirect(url)


@has_journal
@editor_user_required
@require_POST
def regenerate_report(request, article_id, submission_id):
    submission = get_object_or_404(
        models.TurnitinSubmission,
        pk=submission_id,
        article_id=article_id,
        article__journal=request.journal,
    )
    article = submission.article
    article_url = reverse("turnitin_tca_article", args=[article.pk])

    if not logic.is_enabled(request.journal):
        messages.add_message(
            request,
            messages.ERROR,
            "Turnitin is not configured for this journal.",
        )
        return redirect(article_url)
    if not submission.tii_submission_id:
        messages.add_message(
            request,
            messages.ERROR,
            "This file has not been sent to Turnitin yet.",
        )
        return redirect(article_url)

    form = forms.RegenerateReportForm(request.POST)
    if not (form.is_valid() and form.is_confirmed()):
        # First click: render the page with the confirmation modal.
        template = "turnitin_tca/article.html"
        context = logic.article_page_context(article, request.journal)
        context.update(
            {
                "confirmable_button_name": form.CONFIRMABLE_BUTTON_NAME,
                "confirm_modal": form.modal,
                "confirm_form_id": "regen-{}".format(submission.pk),
            }
        )
        return render(request, template, context)

    try:
        logic.request_similarity_report(submission, interactive=True)
        messages.add_message(
            request,
            messages.SUCCESS,
            "A new similarity report was requested. It will appear here once ready.",
        )
    except TurnitinError as exc:
        logic.editor_error(
            request,
            "A new report could not be requested. Please try again later.",
            exc,
        )
    return redirect(article_url)


@csrf_exempt
@require_POST
def webhook(request):
    journal = getattr(request, "journal", None)
    if not journal:
        return HttpResponseBadRequest("No journal context.")

    signature = request.META.get("HTTP_X_TURNITIN_SIGNATURE", "")
    if not logic.verify_webhook_signature(journal, request.body, signature):
        logger.warning("Rejected Turnitin webhook with bad signature.")
        return HttpResponse(status=403)

    event_type = request.META.get("HTTP_X_TURNITIN_EVENTTYPE", "")
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("Malformed payload.")

    logic.handle_webhook_event(event_type, payload, journal)
    return JsonResponse({"status": "ok"})
