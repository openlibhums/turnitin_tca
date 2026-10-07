import base64
import binascii
import hashlib
import hmac
import secrets
from collections import OrderedDict
from datetime import timedelta

import bleach
from bleach_allowlist import bleach_allowlist
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe

from cron.models import Request
from utils import setting_handler, notify_helpers
from utils.logger import get_logger
from utils.logic import get_current_request

from plugins.turnitin_tca import forms, models, plugin_settings
from plugins.turnitin_tca.client import TurnitinClient, TurnitinError

logger = get_logger(__name__)


def get_plugin():
    return plugin_settings.TurnitinTCAPlugin.get_self()


def get_setting(name, journal):
    setting = setting_handler.get_plugin_setting(
        get_plugin(),
        name,
        journal,
        create=True,
    )
    return setting.processed_value


def is_enabled(journal):
    """A journal is configured if the plugin is on and the URL/key are set."""
    if not journal:
        return False
    return bool(
        get_setting("turnitin_enabled", journal)
        and get_setting("turnitin_api_url", journal)
        and get_setting("turnitin_api_key", journal)
    )


def get_client(journal, interactive=False, timeout=None, max_retries=None):
    """Build a TCA client.

    Interactive (user-facing) calls fail fast with a single attempt so a button
    click never hangs through the multi-attempt retry loop; background polling
    keeps the resilient default retries.
    """
    if interactive:
        if max_retries is None:
            max_retries = 1
        if timeout is None:
            timeout = (5, 20)
    return TurnitinClient(
        base_url=get_setting("turnitin_api_url", journal),
        api_key=get_setting("turnitin_api_key", journal),
        integration_name=plugin_settings.INTEGRATION_NAME,
        integration_version=plugin_settings.VERSION,
        timeout=timeout,
        max_retries=max_retries,
    )


def account_external_id(account):
    """A stable, integration-unique reference for a user (max 132 chars)."""
    return "janeway-account-{}".format(account.pk)


def get_article_files(article):
    """Return the files an editor may send for a similarity report.

    Manuscript, data/figure, source, current revision files and the files
    attached to each review round are gathered and de-duplicated by file id,
    preserving a human-meaningful "kind" label for display.
    """
    files = OrderedDict()

    def add(file_obj, kind):
        if file_obj and file_obj.pk not in files:
            files[file_obj.pk] = {"file": file_obj, "kind": kind}

    for f in article.manuscript_files.all():
        add(f, "Manuscript")
    for f in article.data_figure_files.all():
        add(f, "Data/Figure")
    for f in article.source_files.all():
        add(f, "Source")
    # Current revision files (latest manuscript + figure versions).
    for f in article.ms_and_figure_files():
        add(f, "Current revision")
    review_rounds = article.reviewround_set.prefetch_related("review_files")
    for review_round in review_rounds:
        for f in review_round.review_files.all():
            add(f, "Review round {}".format(review_round.round_number))

    return list(files.values())


def submissions_by_file(article):
    """Map file_id -> most recent TurnitinSubmission for that file."""
    mapping = {}
    for submission in article.turnitin_submissions.all().select_related("file"):
        if submission.file_id and submission.file_id not in mapping:
            mapping[submission.file_id] = submission
    return mapping


def group_ref(prefix, obj):
    """A stable, integration-unique reference for a group or group context."""
    return "janeway-{}-{}".format(prefix, obj.pk)


def article_report_rows(article):
    """Rows for the article similarity table: each file with its latest submission."""
    submissions = submissions_by_file(article)
    return [
        {
            "file": entry["file"],
            "kind": entry["kind"],
            "submission": submissions.get(entry["file"].pk),
        }
        for entry in get_article_files(article)
    ]


def failed_rows(rows):
    """The rows whose most recent send to Turnitin failed."""
    return [
        row
        for row in rows
        if row["submission"] and row["submission"].status == "ERROR"
    ]


def article_page_context(article, journal):
    """Context shared by every view that renders the article similarity page."""
    rows = article_report_rows(article)
    return {
        "article": article,
        "rows": rows,
        "failed_rows": failed_rows(rows),
        "enabled": is_enabled(journal),
        "confirmed_button_name": forms.RegenerateReportForm.CONFIRMED_BUTTON_NAME,
        "resend_confirmable_button_name": (
            forms.ResendFailedForm.CONFIRMABLE_BUTTON_NAME
        ),
    }


def build_submission_payload(article, file_obj, submitter, eula_block=None):
    """The Create Submission body for sending ``file_obj`` as ``submitter``.

    The owner is the article's correspondence author (the person whose work
    it is), falling back to the submitter; the submitter is the editor who
    sent the file. The article and journal are sent as group and group
    context so Turnitin can organise submissions the way Janeway does.
    """
    journal = article.journal
    owner = article.correspondence_author or submitter

    payload = {
        "owner": account_external_id(owner),
        "title": (file_obj.label or file_obj.original_filename or "Submission")[:500],
        "submitter": account_external_id(submitter),
        "owner_default_permission_set": "INSTRUCTOR",
        "submitter_default_permission_set": "INSTRUCTOR",
        "metadata": {
            "owners": [
                {
                    "id": account_external_id(owner),
                    "given_name": owner.first_name or "Unknown",
                    "family_name": owner.last_name or "Unknown",
                    "email": owner.email,
                }
            ],
            "submitter": {
                "id": account_external_id(submitter),
                "given_name": submitter.first_name or "Editor",
                "family_name": submitter.last_name or "User",
                "email": submitter.email,
            },
            "group": {
                "id": group_ref("article", article),
                "name": (article.title or "Article {}".format(article.pk))[:500],
                "type": "FOLDER",
            },
            "group_context": {
                "id": group_ref("journal", journal),
                "name": journal.name,
            },
            "original_submitted_time": (
                article.date_submitted or timezone.now()
            ).isoformat(),
        },
    }
    if eula_block:
        payload["eula"] = eula_block
    return payload


def send_client(journal):
    """A client for Create Submission / Upload File.

    Single attempt, so an editor's click never waits through a retry loop (a
    transient failure is retried in the background instead), but with a long
    read window because uploads can be large.
    """
    return get_client(journal, max_retries=1, timeout=(10, 120))


def perform_send(submission, client, eula_block=None):
    """Run whichever of Create Submission and Upload File has not succeeded.

    A row that already has a Turnitin id was created before a failed upload,
    so only the upload is repeated; creating it again would orphan the first
    Turnitin submission.
    """
    article = submission.article
    file_obj = submission.file

    if not submission.tii_submission_id:
        payload = build_submission_payload(
            article,
            file_obj,
            submission.requested_by,
            eula_block,
        )
        created = client.create_submission(payload)
        submission.tii_submission_id = created.get("id")
        submission.status = created.get("status", "CREATED")
        submission.save()

    file_bytes = file_obj.get_file(article, as_bytes=True)
    filename = file_obj.original_filename or "submission"
    client.upload_file(submission.tii_submission_id, file_bytes, filename)
    submission.status = "PROCESSING"
    submission.error_message = None
    submission.next_attempt_at = None
    submission.save()


def mark_send_failed(submission, message):
    submission.status = "ERROR"
    submission.error_message = message
    submission.next_attempt_at = None
    submission.save()


def schedule_retry(submission, exc):
    """Queue a transient send failure for the polling command to retry.

    The delay doubles with each attempt; once MAX_SEND_ATTEMPTS have been
    made the row is marked as failed so an editor can resend it.
    """
    if submission.send_attempts >= plugin_settings.MAX_SEND_ATTEMPTS:
        mark_send_failed(
            submission,
            "Gave up after {} attempts: {}".format(submission.send_attempts, exc),
        )
        return
    delay = plugin_settings.RETRY_BASE_MINUTES * 2 ** (submission.send_attempts - 1)
    submission.status = "RETRYING"
    submission.error_message = str(exc)
    submission.next_attempt_at = timezone.now() + timedelta(minutes=delay)
    submission.save()


def send_file_for_report(article, file_obj, submitter, eula_block=None):
    """Create a TCA submission for ``file_obj`` and upload it.

    The similarity report is requested automatically once the
    SUBMISSION_COMPLETE webhook arrives; the polling fallback covers a missed
    webhook. A transient failure leaves the row RETRYING and returns it; a
    451 (EULA not accepted) or any other client error is raised.
    """
    submission = models.TurnitinSubmission.objects.create(
        article=article,
        file=file_obj,
        file_label=file_obj.label or file_obj.original_filename or "",
        requested_by=submitter,
        status="CREATED",
        send_attempts=1,
    )

    try:
        perform_send(submission, send_client(article.journal), eula_block)
    except TurnitinError as exc:
        if exc.transient:
            logger.warning("Turnitin send failed, will retry: %s", exc)
            schedule_retry(submission, exc)
            return submission
        if exc.eula_required and not submission.tii_submission_id:
            # Nothing was created at Turnitin; the send is repeated once the
            # user has accepted the EULA, so this row would only be noise.
            logger.info("Turnitin send needs EULA acceptance: %s", exc)
            submission.delete()
            raise
        logger.error("Turnitin send failed: %s", exc)
        mark_send_failed(submission, str(exc))
        raise

    return submission


def latest_eula_block(account):
    """An eula block from the account's most recent recorded acceptance."""
    if not account:
        return None
    acceptance = (
        models.TurnitinEulaAcceptance.objects.filter(account=account)
        .order_by("-accepted_timestamp")
        .first()
    )
    if not acceptance:
        return None
    return {
        "version": acceptance.version,
        "accepted_timestamp": acceptance.accepted_timestamp.isoformat(),
        "language": acceptance.language,
    }


def retry_send(submission):
    """Retry a send in the background (polling command or bulk resend)."""
    if not submission.file or not submission.requested_by:
        mark_send_failed(
            submission,
            "The file or the editor who sent it no longer exists.",
        )
        return submission

    submission.send_attempts += 1
    submission.save()
    try:
        perform_send(
            submission,
            send_client(submission.article.journal),
            latest_eula_block(submission.requested_by),
        )
    except TurnitinError as exc:
        if exc.transient:
            logger.warning(
                "Turnitin send retry %s failed for %s: %s",
                submission.send_attempts,
                submission.pk,
                exc,
            )
            schedule_retry(submission, exc)
        elif exc.eula_required:
            logger.error("Turnitin send %s needs EULA acceptance", submission.pk)
            mark_send_failed(
                submission,
                "The sending editor must accept the Turnitin EULA; resend the "
                "file from the article page.",
            )
        else:
            logger.error("Turnitin send retry failed for %s: %s", submission.pk, exc)
            mark_send_failed(submission, str(exc))
    return submission


def resend_submission(submission):
    """Resend a failed submission's file as a new submission, in the background.

    The original row is kept as a record of the failure; the new row carries
    the same file and requesting editor.
    """
    fresh = models.TurnitinSubmission.objects.create(
        article=submission.article,
        file=submission.file,
        file_label=submission.file_label,
        requested_by=submission.requested_by,
        status="RETRYING",
    )
    return retry_send(fresh)


def latest_failed_submissions(queryset):
    """From ``queryset``, the submissions that are the newest for their file
    and whose send failed."""
    seen = set()
    failed = []
    for submission in queryset.filter(file__isnull=False).order_by("-date_created"):
        if submission.file_id in seen:
            continue
        seen.add(submission.file_id)
        if submission.status == "ERROR":
            failed.append(submission)
    return failed


def required_eula_block(journal, account):
    """The eula block a send must carry when the journal requires the EULA.

    Returns (block, accepted). When the journal does not present the EULA
    this is (None, True). When it does and the account has not accepted the
    latest version, it is (None, False) and the caller should send the user
    to the EULA page. Raises TurnitinError if the EULA cannot be fetched.
    """
    if not eula_required(journal):
        return None, True
    version = get_latest_eula(journal).get("version")
    if not user_has_accepted_eula(account, version):
        return None, False
    return build_eula_block(account, version), True


def send_file_from_request(request, article, file_obj, eula_block=None):
    """Send a file for an editor and tell them what happened.

    Returns the URL to redirect to: the EULA page when Turnitin answers 451,
    otherwise the article page.
    """
    article_url = reverse("turnitin_tca_article", args=[article.pk])
    try:
        submission = send_file_for_report(article, file_obj, request.user, eula_block)
    except TurnitinError as exc:
        if exc.eula_required:
            request.session["turnitin_pending_file"] = file_obj.pk
            messages.add_message(
                request,
                messages.INFO,
                "Turnitin requires you to accept its End User Licence "
                "Agreement before this file can be sent.",
            )
            return reverse("turnitin_tca_eula", args=[article.pk])
        messages.add_message(
            request,
            messages.ERROR,
            "Send failed: {}".format(exc),
        )
        return article_url

    if submission.status == "RETRYING":
        messages.add_message(
            request,
            messages.WARNING,
            "Turnitin could not be reached. The file will be sent again "
            "automatically; its status will update here.",
        )
    else:
        messages.add_message(
            request,
            messages.SUCCESS,
            "File sent to Turnitin. The report will appear here once ready.",
        )
    return article_url


def request_similarity_report(submission, interactive=False):
    """Ask TCA to generate the similarity report for a processed submission.

    Background callers (webhook, polling) use the resilient retrying client;
    an editor re-requesting a report from the page passes interactive=True so
    the button click fails fast rather than hanging on the retry loop.
    """
    journal = submission.article.journal
    client = get_client(journal, interactive=interactive)
    settings = {
        "indexing_settings": {
            "add_to_index": bool(get_setting("turnitin_index_submissions", journal)),
        },
        "generation_settings": {
            "search_repositories": plugin_settings.DEFAULT_SEARCH_REPOSITORIES,
            "auto_exclude_self_matching_scope": "ALL",
        },
        "view_settings": {
            "exclude_quotes": False,
            "exclude_bibliography": False,
        },
    }
    submission.report_attempts += 1
    submission.save()
    try:
        client.generate_similarity_report(submission.tii_submission_id, settings)
        submission.report_requested = True
        submission.report_status = "PROCESSING"
        submission.save()
    except TurnitinError as exc:
        logger.error("Turnitin report request failed: %s", exc)
        submission.report_status = "ERROR"
        submission.error_message = str(exc)
        submission.save()
        raise


def can_auto_request_report(submission):
    """Whether the polling fallback may (re)request this submission's report.

    Automatic requests stop after MAX_REPORT_ATTEMPTS so a report Turnitin
    keeps refusing is not requested forever; an editor can still ask again
    with "Request report again".
    """
    return (
        not submission.report_requested
        and submission.report_attempts < plugin_settings.MAX_REPORT_ATTEMPTS
    )


def poll_submission(submission):
    """Pull current status from TCA for one submission (webhook fallback)."""
    journal = submission.article.journal
    client = get_client(journal)

    info = client.submission_info(submission.tii_submission_id)
    submission.status = info.get("status", submission.status)
    if submission.status == "COMPLETE" and can_auto_request_report(submission):
        try:
            request_similarity_report(submission)
        except TurnitinError:
            pass
    elif submission.status == "ERROR":
        submission.error_message = info.get("error_code", "Submission error")
    submission.save()

    if submission.report_requested and submission.report_status != "COMPLETE":
        poll_report(submission, client)
    return submission


def poll_report(submission, client):
    info = client.similarity_report_info(submission.tii_submission_id)
    overall = info.get("overall_match_percentage")
    score = int(round(float(overall))) if overall is not None else None
    apply_report_result(
        submission,
        info.get("status", submission.report_status),
        score,
    )
    return submission


def apply_report_result(submission, status, score):
    """Persist a similarity-report result idempotently and without regression.

    Both the webhook and the polling fallback can observe a report, and either
    can arrive stale (e.g. a poll that started before a webhook completed the
    report). A conditional update means whichever process sees COMPLETE first
    wins and a later, older observation cannot overwrite it. The completion
    email is sent at most once, via notify_report_complete's atomic claim.
    """
    fields = {"report_status": status}
    if score is not None:
        fields["similarity_score"] = score

    updated = (
        models.TurnitinSubmission.objects.filter(
            pk=submission.pk,
        )
        .exclude(
            report_status="COMPLETE",
        )
        .update(**fields)
    )

    if updated:
        submission.report_status = status
        if score is not None:
            submission.similarity_score = score
    else:
        # The report is already COMPLETE in the database; reflect that locally
        # rather than clobbering it with this (older) observation.
        submission.refresh_from_db(
            fields=["report_status", "similarity_score", "report_notified"],
        )

    if submission.report_status == "COMPLETE":
        notify_report_complete(submission)


def get_viewer_url(submission, user):
    """Return a launch URL for the similarity report viewer."""
    journal = submission.article.journal
    client = get_client(journal, interactive=True)
    payload = {
        "viewer_user_id": account_external_id(user),
        "locale": "en-US",
        "viewer_default_permission_set": "INSTRUCTOR",
    }
    response = client.viewer_url(submission.tii_submission_id, payload)
    return response.get("viewer_url")


def eula_required(journal):
    return bool(get_setting("turnitin_eula_acceptance", journal))


def get_latest_eula(journal):
    client = get_client(journal)
    return client.eula_version_info("latest")


def sanitize_eula_html(eula_html):
    """Sanitize the EULA HTML Turnitin returns before it is rendered.

    The EULA page is fetched over HTTP with no signature to verify it, so it is
    treated as untrusted third-party content: it is cleaned against an allowlist
    of formatting tags (no scripts, event handlers or styles) so a tampered or
    misconfigured endpoint cannot inject script into an editor's session.
    """
    cleaned = bleach.clean(
        eula_html or "",
        tags=bleach_allowlist.print_tags,
        attributes={"a": ["href", "title", "target", "rel"]},
        strip=True,
    )
    return mark_safe(cleaned)


def user_has_accepted_eula(account, version):
    return models.TurnitinEulaAcceptance.objects.filter(
        account=account,
        version=version,
    ).exists()


def record_eula_acceptance(account, version, language="en-US"):
    acceptance, _ = models.TurnitinEulaAcceptance.objects.get_or_create(
        account=account,
        version=version,
        defaults={"language": language, "accepted_timestamp": timezone.now()},
    )
    return acceptance


def accept_eula(journal, account, version, language="en-US"):
    """Record EULA acceptance locally and with Turnitin.

    Recording it with Turnitin means a later request for this user (a send or
    a viewer launch) is not refused with a 451. The local record is kept even
    if the Turnitin call raises.
    """
    acceptance = record_eula_acceptance(account, version, language)
    get_client(journal, interactive=True).accept_eula(
        version,
        account_external_id(account),
        acceptance.accepted_timestamp.isoformat(),
        language,
    )
    return acceptance


def build_eula_block(account, version, language="en-US"):
    acceptance = record_eula_acceptance(account, version, language)
    return {
        "version": version,
        "accepted_timestamp": acceptance.accepted_timestamp.isoformat(),
        "language": language,
    }


def save_setting(name, value, journal):
    setting_handler.save_plugin_setting(get_plugin(), name, value, journal)


def generate_signing_secret():
    """A fresh Base64-encoded signing secret, as required by TCA."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def get_or_create_signing_secret(journal):
    secret = get_setting("turnitin_webhook_secret", journal)
    if not secret:
        secret = generate_signing_secret()
        save_setting("turnitin_webhook_secret", secret, journal)
    return secret


def signing_key(secret):
    """TCA signing secrets are Base64; the HMAC key is the decoded bytes."""
    try:
        return base64.b64decode(secret, validate=True)
    except (binascii.Error, ValueError):
        # Fall back to the raw secret for any non-Base64 legacy value.
        return secret.encode("utf-8")


def register_webhook(journal):
    """Register (or re-register) the callback URL with Turnitin.

    There is a limit of 10 webhooks per integration scope, so any previously
    registered webhook is deleted first to avoid accumulating duplicates.
    """
    client = get_client(journal, interactive=True)
    secret = get_or_create_signing_secret(journal)
    url = get_setting("turnitin_webhook_url", journal) or journal.site_url(
        path=reverse("turnitin_tca_webhook"),
    )

    existing_id = get_setting("turnitin_webhook_id", journal)
    if existing_id:
        try:
            client.delete_webhook(existing_id)
        except TurnitinError as exc:
            logger.warning("Could not delete old webhook %s: %s", existing_id, exc)

    payload = {
        "signing_secret": secret,
        "description": "Janeway: {}".format(journal.name),
        "url": url,
        "event_types": plugin_settings.SUBSCRIBED_EVENTS,
        "allow_insecure": False,
    }
    result = client.create_webhook(payload)
    save_setting("turnitin_webhook_id", result.get("id", ""), journal)
    return result


def list_webhooks(journal):
    """All webhooks currently registered at Turnitin for this scope."""
    return get_client(journal, interactive=True).list_webhooks()


def delete_webhook(journal, webhook_id):
    """Delete a single webhook and forget it if it was the registered one."""
    get_client(journal, interactive=True).delete_webhook(webhook_id)
    if get_setting("turnitin_webhook_id", journal) == webhook_id:
        save_setting("turnitin_webhook_id", "", journal)


def delete_all_webhooks(journal):
    """Delete every webhook registered for this scope. Returns the count."""
    client = get_client(journal, interactive=True)
    hooks = client.list_webhooks()
    for hook in hooks:
        client.delete_webhook(hook["id"])
    save_setting("turnitin_webhook_id", "", journal)
    return len(hooks)


def verify_webhook_signature(journal, body_bytes, signature):
    secret = get_setting("turnitin_webhook_secret", journal)
    if not secret:
        # No secret configured: cannot verify, reject for safety.
        return False
    expected = hmac.new(
        signing_key(secret),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


def handle_webhook_event(event_type, payload, journal):
    """Advance a submission's state in response to a TCA webhook event.

    The lookup is scoped to ``journal`` so that a signature valid for one
    journal can only ever mutate that journal's own submissions, never another
    tenant's (see #4313-adjacent hardening; the signature proves the caller
    holds this journal's secret, not that the submission belongs to it).
    """
    submission_id = payload.get("id") or payload.get("submission_id")
    if not submission_id:
        return None

    try:
        submission = models.TurnitinSubmission.objects.get(
            tii_submission_id=submission_id,
            article__journal=journal,
        )
    except models.TurnitinSubmission.DoesNotExist:
        logger.warning(
            "Turnitin webhook for unknown submission %s on journal %s",
            submission_id,
            journal.code,
        )
        return None

    submission.last_webhook = timezone.now()

    if event_type == "SUBMISSION_COMPLETE":
        status = payload.get("status", "COMPLETE")
        submission.status = status
        submission.save()
        if status == "COMPLETE" and can_auto_request_report(submission):
            try:
                request_similarity_report(submission)
            except TurnitinError:
                pass
        elif status == "ERROR":
            submission.error_message = payload.get("error_code", "Submission error")
            submission.save()
    elif event_type in ("SIMILARITY_COMPLETE", "SIMILARITY_UPDATED"):
        overall = payload.get("overall_match_percentage")
        score = int(round(float(overall))) if overall is not None else None
        apply_report_result(
            submission,
            payload.get("status", "COMPLETE"),
            score,
        )
    else:
        submission.save()

    return submission


def notify_report_complete(submission):
    """Email the editor who requested the report that it is ready.

    The notification is claimed with a single conditional update so that a
    duplicate webhook delivery or an overlapping poll cannot email the editor
    more than once: only the first caller to flip ``report_notified`` proceeds.
    """
    if not submission.requested_by:
        return

    claimed = models.TurnitinSubmission.objects.filter(
        pk=submission.pk,
        report_notified=False,
    ).update(report_notified=True)
    if not claimed:
        return
    submission.report_notified = True

    article = submission.article
    journal = article.journal

    request = get_current_request()
    if not request or getattr(request, "journal", None) != journal:
        request = Request()
        request.journal = journal
        request.site_type = journal

    article_url = journal.site_url(
        path=reverse("turnitin_tca_article", kwargs={"article_id": article.pk}),
    )
    score = (
        "{}%".format(submission.similarity_score)
        if submission.similarity_score is not None
        else "available"
    )
    body = (
        "<p>Dear {name},</p>"
        "<p>The Turnitin similarity report for the file "
        "<strong>{label}</strong> on article "
        "<strong>{title}</strong> is now {score}.</p>"
        "<p>You can view the report here: "
        '<a href="{url}">{url}</a></p>'
    ).format(
        name=submission.requested_by.full_name(),
        label=submission.file_label or "manuscript",
        title=article.title,
        score=score,
        url=article_url,
    )

    log_dict = {
        "level": "Info",
        "action_text": "Turnitin similarity report ready",
        "types": "Turnitin Similarity Report",
        "target": article,
    }
    notify_helpers.send_email_with_body_from_user(
        request,
        "Turnitin similarity report ready",
        submission.requested_by.email,
        body,
        log_dict=log_dict,
    )
