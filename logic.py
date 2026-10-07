import base64
import binascii
import hashlib
import hmac
import json
import secrets
import uuid
from collections import OrderedDict
from datetime import timedelta

import bleach
from bleach_allowlist import bleach_allowlist
from django.contrib import messages
from django.urls import get_resolver, reverse
from django.utils import timezone
from django.utils.safestring import mark_safe

from core import models as core_models
from cron.models import Request
from press import models as press_models
from utils import setting_handler, notify_helpers
from utils.logger import get_logger
from utils.logic import get_current_request

from plugins.turnitin_tca import forms, models, plugin_settings
from plugins.turnitin_tca.client import TurnitinClient, TurnitinError

logger = get_logger(__name__)


def get_plugin():
    return plugin_settings.TurnitinTCAPlugin.get_self()


def setting_journal(name, journal):
    """The journal a setting is stored against: None for install-wide ones."""
    if name in plugin_settings.INSTALL_SETTING_NAMES:
        return None
    return journal


def get_setting(name, journal):
    setting = setting_handler.get_plugin_setting(
        get_plugin(),
        name,
        setting_journal(name, journal),
        create=True,
    )
    return setting.processed_value


def is_configured():
    """Whether staff have entered the install-wide API URL and key."""
    return bool(
        get_setting("turnitin_api_url", None) and get_setting("turnitin_api_key", None)
    )


def is_enabled(journal):
    """A journal is configured if the plugin is on and the URL/key are set."""
    if not journal:
        return False
    return bool(get_setting("turnitin_enabled", journal) and is_configured())


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


def editor_error(request, message, exc=None):
    """Show an editor a user-friendly error, keeping Turnitin's detail for admins.

    The API's own error text is not meant for editorial staff, so it is logged
    and only appended to the message for staff users (Janeway admins), who can
    use it to raise a support ticket.
    """
    if exc is not None:
        logger.error("%s %s", message, exc)
        if request.user.is_staff:
            message = "{} (Turnitin said: {})".format(message, exc)
    messages.add_message(request, messages.ERROR, message)


def tenant_requires_eula(features):
    """Read tenant.require_eula from a Get Features Enabled response."""
    return bool((features.get("tenant") or {}).get("require_eula"))


def refresh_features(journal):
    """Call Get Features Enabled and store whether the tenant requires the EULA.

    Customers who bought iThenticate directly from Turnitin must be shown the
    EULA in the submission workflow; those who bought it through Crossref must
    not. Turnitin decides which, so the answer is stored rather than being an
    editor setting. On failure the stored value is cleared, so it is fetched
    again before the next send, and the error is raised.
    """
    try:
        features = get_client(journal, interactive=True).features_enabled()
    except TurnitinError:
        save_setting(plugin_settings.REQUIRE_EULA_SETTING, "", journal)
        raise
    save_setting(
        plugin_settings.REQUIRE_EULA_SETTING,
        "true" if tenant_requires_eula(features) else "false",
        journal,
    )
    return features


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
        row for row in rows if row["submission"] and row["submission"].status == "ERROR"
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


def owner_metadata(article):
    """The Create Submission owner id and metadata for ``article``.

    The owner is the article's correspondence author (the person whose work
    it is). Janeway requires one, but the field is nullable (e.g. if the
    author's account is deleted), so as a safeguard Turnitin's guidance is
    followed: a random UUID with the name "Anonymous Author", never the
    editor sending the file.
    """
    owner = article.correspondence_author
    if not owner:
        owner_id = str(uuid.uuid4())
        return owner_id, {
            "id": owner_id,
            "given_name": "Anonymous",
            "family_name": "Author",
        }
    owner_id = account_external_id(owner)
    return owner_id, {
        "id": owner_id,
        "given_name": owner.first_name or "Unknown",
        "family_name": owner.last_name or "Unknown",
        "email": owner.email,
    }


def build_submission_payload(article, file_obj, submitter, eula_block=None):
    """The Create Submission body for sending ``file_obj`` as ``submitter``.

    The submitter is the editor who sent the file. The article and journal
    are sent as group and group context so Turnitin can organise submissions
    the way Janeway does.
    """
    journal = article.journal
    owner_id, owner = owner_metadata(article)

    payload = {
        "owner": owner_id,
        "title": (file_obj.label or file_obj.original_filename or "Submission")[:500],
        "submitter": account_external_id(submitter),
        # INSTRUCTOR is reserved for Turnitin's LMS integrations.
        "owner_default_permission_set": "UNDEFINED",
        "submitter_default_permission_set": "EDITOR",
        "metadata": {
            "owners": [owner],
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
            # Returned in webhook events, so an event can be matched to its
            # journal.
            "custom": json.dumps({"journal": journal.code}),
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
            models.MISSING_FILE_MESSAGE,
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
            mark_send_failed(submission, models.EULA_RESEND_MESSAGE)
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
        editor_error(
            request,
            "The file could not be sent to Turnitin. Please try again later.",
            exc,
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


def apply_report_result(submission, status, score, updated_report=False):
    """Persist a similarity-report result idempotently and without regression.

    Both the webhook and the polling fallback can observe a report, and either
    can arrive stale (e.g. a poll that started before a webhook completed the
    report). A conditional update means whichever process sees COMPLETE first
    wins and a later, older observation cannot overwrite it. The completion
    email is sent at most once, via notify_report_complete's atomic claim.

    ``updated_report`` is set for SIMILARITY_UPDATED, which Turnitin sends
    when an editor's saved viewer changes alter the score of a report that is
    already complete, so it overwrites the stored score.
    """
    # Turnitin reports an overall_match_percentage of 0 while a report is
    # still processing, which is not a real score.
    if status != "COMPLETE":
        score = None
    fields = {"report_status": status}
    if score is not None:
        fields["similarity_score"] = score

    rows = models.TurnitinSubmission.objects.filter(pk=submission.pk)
    if not updated_report:
        rows = rows.exclude(report_status="COMPLETE")
    updated = rows.update(**fields)

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
    """Return a launch URL for the similarity report viewer.

    The optional eula attributes are never sent: the viewer presents the
    latest EULA itself when the tenant requires it. save_changes keeps an
    editor's changes in the viewer and makes Turnitin send SIMILARITY_UPDATED
    when they change the overall score.
    """
    journal = submission.article.journal
    client = get_client(journal, interactive=True)
    payload = {
        "viewer_user_id": account_external_id(user),
        "locale": "en-US",
        "viewer_default_permission_set": "EDITOR",
        "similarity": {
            "view_settings": {
                "save_changes": bool(
                    get_setting("turnitin_save_report_changes", journal),
                ),
            },
        },
    }
    response = client.viewer_url(submission.tii_submission_id, payload)
    return response.get("viewer_url")


def eula_required(journal):
    """Whether the tenant requires the EULA in the submission workflow.

    Uses the stored tenant.require_eula, fetching it first if it is not yet
    known. If Turnitin cannot be reached the send goes ahead; a tenant that
    does require the EULA answers 451, which sends the editor to the EULA.
    """
    value = get_setting(plugin_settings.REQUIRE_EULA_SETTING, journal)
    if value:
        return value == "true"
    try:
        return tenant_requires_eula(refresh_features(journal))
    except TurnitinError as exc:
        logger.warning("Could not check whether Turnitin requires the EULA: %s", exc)
        return False


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
    setting_handler.save_plugin_setting(
        get_plugin(),
        name,
        value,
        setting_journal(name, journal),
    )


def adopt_journal_settings():
    """Copy account settings saved per journal to the install-wide settings.

    Earlier versions stored the API URL and key on each journal. If the
    install-wide setting is empty, use the first journal's value.
    """
    for name in plugin_settings.SETTING_NAMES:
        if get_setting(name, None):
            continue
        value = (
            core_models.SettingValue.objects.filter(
                setting__group__name="plugin:{}".format(get_plugin().name),
                setting__name=name,
                journal__isnull=False,
            )
            .exclude(value__isnull=True)
            .exclude(value="")
            .order_by("journal__pk")
            .values_list("value", flat=True)
            .first()
        )
        if value:
            save_setting(name, value, None)


def generate_signing_secret():
    """A fresh Base64-encoded signing secret, as required by TCA."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def signing_key(secret):
    """TCA signing secrets are Base64; the HMAC key is the decoded bytes."""
    try:
        return base64.b64decode(secret, validate=True)
    except (binascii.Error, ValueError):
        # Fall back to the raw secret for any non-Base64 legacy value.
        return secret.encode("utf-8")


def install_url(name):
    """A plugin URL at the press level, from a journal or press request."""
    # Reverse without the journal prefix a journal request adds.
    path = "/" + get_resolver().reverse(name)
    return press_models.Press.objects.first().site_url(path=path)


def webhook_url():
    """The single callback URL this install registers with Turnitin.

    Turnitin sends every event to every webhook registered for an API key, so
    the install registers one URL, at the press level, for all its journals.
    """
    override = get_setting("turnitin_webhook_url", None)
    if override:
        return override
    return install_url("turnitin_tca_webhook")


def journal_webhook_ids():
    """Webhooks registered per journal by earlier versions of the plugin."""
    return set(
        core_models.SettingValue.objects.filter(
            setting__group__name="plugin:{}".format(get_plugin().name),
            setting__name="turnitin_webhook_id",
            journal__isnull=False,
        )
        .exclude(value__isnull=True)
        .exclude(value="")
        .values_list("value", flat=True)
    )


def register_webhook(journal=None):
    """Register this install's webhook with Turnitin, unless it already is.

    If the webhook URL is already registered with the secret we hold, nothing
    is changed. A webhook for our URL whose secret we no longer hold cannot be
    verified, so it is replaced, as are the per-journal webhooks registered by
    earlier versions of the plugin. Returns the webhook and whether it was
    created.
    """
    client = get_client(None, interactive=True)
    url = webhook_url()
    hooks = client.list_webhooks()
    registered_id = get_setting("turnitin_webhook_id", None)
    secret = get_setting("turnitin_webhook_secret", None)

    for hook in hooks:
        if hook.get("url") == url and hook.get("id") == registered_id and secret:
            return hook, False

    replace = journal_webhook_ids()
    for hook in hooks:
        if hook.get("url") == url or hook.get("id") in replace:
            try:
                client.delete_webhook(hook["id"])
            except TurnitinError as exc:
                logger.warning("Could not delete old webhook %s: %s", hook["id"], exc)

    secret = generate_signing_secret()
    save_setting("turnitin_webhook_secret", secret, None)
    payload = {
        "signing_secret": secret,
        "description": "Janeway",
        "url": url,
        "event_types": plugin_settings.SUBSCRIBED_EVENTS,
        "allow_insecure": False,
    }
    result = client.create_webhook(payload)
    save_setting("turnitin_webhook_id", result.get("id", ""), None)
    return result, True


def list_webhooks(journal=None):
    """All webhooks currently registered at Turnitin for this scope."""
    return get_client(journal, interactive=True).list_webhooks()


def delete_webhook(journal, webhook_id):
    """Delete a single webhook and forget it if it was the registered one."""
    get_client(journal, interactive=True).delete_webhook(webhook_id)
    if get_setting("turnitin_webhook_id", journal) == webhook_id:
        save_setting("turnitin_webhook_id", "", journal)


def delete_all_webhooks(journal=None):
    """Delete every webhook registered for this scope. Returns the count."""
    client = get_client(journal, interactive=True)
    hooks = client.list_webhooks()
    for hook in hooks:
        client.delete_webhook(hook["id"])
    save_setting("turnitin_webhook_id", "", journal)
    return len(hooks)


def verify_webhook_signature(body_bytes, signature):
    secret = get_setting("turnitin_webhook_secret", None)
    if not secret:
        # No secret configured: cannot verify, reject for safety.
        return False
    expected = hmac.new(
        signing_key(secret),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


def event_journal_code(payload):
    """The journal code sent in metadata.custom with Create Submission."""
    custom = (payload.get("metadata") or {}).get("custom")
    try:
        return (json.loads(custom) or {}).get("journal")
    except (TypeError, ValueError, AttributeError):
        return None


def handle_webhook_event(event_type, payload, journal=None):
    """Advance a submission's state in response to a TCA webhook event.

    One webhook serves the whole install, so the submission is found by its
    Turnitin ID. When the event carries the journal code from metadata.custom,
    or arrives at a journal's URL, the submission must belong to that journal.
    """
    submission_id = payload.get("id") or payload.get("submission_id")
    if not submission_id:
        return None

    submissions = models.TurnitinSubmission.objects.filter(
        tii_submission_id=submission_id,
    )
    if journal:
        submissions = submissions.filter(article__journal=journal)
    code = event_journal_code(payload)
    if code:
        submissions = submissions.filter(article__journal__code=code)
    submission = submissions.first()
    if not submission:
        logger.warning(
            "Turnitin webhook for unknown submission %s (journal %s)",
            submission_id,
            code or (journal.code if journal else "not given"),
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
            updated_report=event_type == "SIMILARITY_UPDATED",
        )
        # apply_report_result saves with a queryset update, so record the
        # webhook time separately.
        models.TurnitinSubmission.objects.filter(pk=submission.pk).update(
            last_webhook=submission.last_webhook,
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
