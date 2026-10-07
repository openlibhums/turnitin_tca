import os

from django.conf import settings

from utils import plugins
from utils.install import update_settings
from utils.logger import get_logger

logger = get_logger(__name__)

PLUGIN_NAME = "Turnitin Core API"
DESCRIPTION = (
    "Send manuscript and revision files to Turnitin Core API (TCA) for "
    "similarity reports and view the results from the editorial workflow."
)
AUTHOR = "Open Library of Humanities"
VERSION = "0.1"
# Sent as the X-Turnitin-Integration-Name / -Version headers, which identify
# this integration (not the journal) to Turnitin.
INTEGRATION_NAME = "Janeway"
SHORT_NAME = "turnitin_tca"
DISPLAY_NAME = "Turnitin Core API"
MANAGER_URL = "turnitin_tca_manager"
JANEWAY_VERSION = "1.7.0"
IS_WORKFLOW_PLUGIN = False

# Plugin settings editable from the manager form. The webhook secret and id are
# managed by the webhook registration flow, not edited by hand.
SETTING_NAMES = [
    "turnitin_enabled",
    "turnitin_api_url",
    "turnitin_api_key",
    "turnitin_index_submissions",
    "turnitin_save_report_changes",
    "turnitin_webhook_url",
]

# Whether the tenant requires EULA acceptance is not an editor choice: it is
# read from Turnitin's Get Features Enabled response (tenant.require_eula)
# whenever the API URL and key are saved, and stored here as "true"/"false"
# ("" until it has been fetched).
REQUIRE_EULA_SETTING = "turnitin_require_eula"

# The default repositories a similarity report is generated against.
DEFAULT_SEARCH_REPOSITORIES = [
    "INTERNET",
    "PUBLICATION",
    "CROSSREF",
    "CROSSREF_POSTED_CONTENT",
    "SUBMITTED_WORK",
]

# A send that fails on a transient error (network, 429, 5xx) is retried in the
# background by the polling command, backing off from RETRY_BASE_MINUTES and
# doubling each time, until MAX_SEND_ATTEMPTS have been made.
MAX_SEND_ATTEMPTS = 5
RETRY_BASE_MINUTES = 10

# A failed Generate Similarity Report request is retried by the polling
# command (every POLL_EVERY_MINUTES) until this many attempts have been made.
MAX_REPORT_ATTEMPTS = 5

# The webhook event types this plugin subscribes to and handles.
SUBSCRIBED_EVENTS = [
    "SUBMISSION_COMPLETE",
    "SIMILARITY_COMPLETE",
    "SIMILARITY_UPDATED",
]


class TurnitinTCAPlugin(plugins.Plugin):
    plugin_name = PLUGIN_NAME
    display_name = DISPLAY_NAME
    description = DESCRIPTION
    author = AUTHOR
    short_name = SHORT_NAME

    manager_url = MANAGER_URL

    version = VERSION
    janeway_version = JANEWAY_VERSION

    is_workflow_plugin = IS_WORKFLOW_PLUGIN


# Webhooks are the primary mechanism for advancing submissions. This polling
# fallback only covers the rare missed webhook, so a relaxed ~20 minute cadence
# is sufficient (the command itself ignores submissions younger than 30 minutes).
POLL_EVERY_MINUTES = 20
CRON_TASK = "poll_turnitin_tca"


def install():
    TurnitinTCAPlugin.install()
    update_settings(
        file_path="plugins/turnitin_tca/install/settings.json",
    )
    install_cron()


def cron_job_name():
    """A crontab comment unique to this Janeway install and plugin."""
    project_slug = settings.PROJECT_DIR.replace("/", "_")
    return "{}_janeway_{}".format(project_slug, CRON_TASK)


def install_cron(action=""):
    """Register the polling fallback in the user's crontab.

    Mirrors core's install_cron: it is idempotent (keyed on a unique comment),
    safe to re-run, and a no-op where crontab is unavailable or during tests so
    a developer's real crontab is never mutated by the test suite.
    """
    if settings.IN_TEST_RUNNER:
        return

    try:
        import crontab
    except (ImportError, ModuleNotFoundError):
        logger.warning("crontab module not installed; skipping cron install.")
        return

    if not os.path.isfile("/usr/bin/crontab"):
        logger.warning("/usr/bin/crontab not found; skipping cron install.")
        return

    tab = crontab.CronTab(user=True)
    name = cron_job_name()

    for job in tab:
        if job.comment == name:
            logger.info("Turnitin cron job already exists.")
            if action == "test":
                print(tab.render())
            return

    django_command = "{0}/manage.py {1}".format(settings.BASE_DIR, CRON_TASK)
    virtualenv = os.environ.get("VIRTUAL_ENV", None)
    if virtualenv:
        command = "{0}/bin/python3 {1}".format(virtualenv, django_command)
    else:
        command = django_command

    cron_job = tab.new(command, comment=name)
    cron_job.minute.every(POLL_EVERY_MINUTES)

    if action == "test":
        print(tab.render())
    else:
        tab.write()


def hook_registry():
    return {
        "unassigned_additional_actions": {
            "module": "plugins.turnitin_tca.hooks",
            "function": "review_action_hook",
        },
        "review_actions": {
            "module": "plugins.turnitin_tca.hooks",
            "function": "review_action_hook",
        },
    }


def register_for_events():
    pass
