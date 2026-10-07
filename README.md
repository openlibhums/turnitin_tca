# Turnitin Core API plugin

Sends manuscript, data/figure, source, revision and review-round files to the [Turnitin Core API](https://developers.turnitin.com/turnitin-core-api) (TCA) for similarity reports, and surfaces the results from the editorial workflow.

## Features

- Adds a **Turnitin Similarity** action to the editor Actions block on both the Unassigned and In Review pages (via the `unassigned_additional_actions` and `review_actions` hooks).
- A per-article page listing every file an editor can send, with the current report status, similarity score, and a link to the Turnitin viewer.
- Sends a file (Create Submission then Upload File) and, once Turnitin reports the upload as processed, automatically requests the similarity report.
- A webhook receiver (`SUBMISSION_COMPLETE` / `SIMILARITY_COMPLETE`) that stores the score and emails the requesting editor. Incoming webhooks are verified against a shared signing secret. This is the primary mechanism for advancing submissions.
- A polling fallback (`poll_turnitin_tca` management command) for the rare submission that does not receive a webhook. Installation registers this in the crontab automatically to run every 20 minutes.
- EULA presentation that follows the Turnitin licence rather than a setting. Saving the API URL and key calls **Get Features Enabled**, and `tenant.require_eula` decides whether editors must accept the EULA before sending a file (iThenticate bought directly from Turnitin) or never see it (iThenticate bought through Crossref). The viewer launch never sends EULA attributes; the report viewer presents the EULA itself when needed. A `451` from Turnitin still sends the editor to the EULA page and the send (or viewer launch) continues once they accept.
- A **Save iThenticate report change** setting, on by default, that keeps changes made in the report viewer (`save_changes`). When the overall match percentage changes, the `SIMILARITY_UPDATED` webhook updates the stored score.
- Background retry of sends that fail on a transient error (network, `429` or `5xx`), and **Resend** / **Resend all failed** actions for sends that fail permanently.
- Submissions carry the article as their `group` and the journal as their `group_context`. The owner is the article's correspondence author; Janeway requires one, but should it ever be missing the submission is sent as "Anonymous Author" with a random UUID, never as the sending editor. Owner, submitter and viewer permission sets are `UNDEFINED`, `EDITOR` and `EDITOR`.
- Editors see user-friendly error messages. Turnitin's own error text is logged and shown only to staff (Janeway admins), so they can raise a support ticket.

## Installation

```bash
python src/manage.py install_plugins turnitin_tca
python src/manage.py migrate turnitin_tca
```

The Turnitin account is set once for the whole install. As a staff user, open **Turnitin Core API** from the press-level plugin list (`/plugins/turnitin-core-api/` on the press domain) and enter the API URL and API key. Saving checks the connection and whether your licence requires the EULA, then registers the install's webhook; **Test Connection** and **Register Webhook** repeat these. Each journal then turns the integration on from its own **Turnitin Core API** page, which shows the install's EULA status.

Turnitin sends every event to every webhook registered for an API key, so the install registers a single callback URL (`/plugins/turnitin-core-api/webhook/` on the press domain), and only if it is not already registered. Registration calls `POST /api/v1/webhooks` with a Base64 signing secret that is generated for you; that secret verifies the `X-Turnitin-Signature` on incoming callbacks. Each submission sends its journal code in `metadata.custom`, so the receiver can check the event belongs to that journal. Earlier versions stored the account and a webhook on each journal: `install_plugins` copies the first journal's account settings to the install, and registering replaces the old per-journal webhooks.

## Developing and testing locally

Turnitin requires the webhook callback URL to be HTTPS and publicly reachable, so a `http://localhost` dev box cannot receive callbacks. Webhook handling is covered by the test suite, which posts a dummy signed payload to the receiver and asserts the resulting state change. Interactive calls (Test Connection, Register Webhook, View Report) use a single attempt with short timeouts so a button never hangs. Errors from Turnitin (including the 4xx body) are logged and shown to staff users; other editors see a plain message.

To receive real callbacks on a dev box, expose the server with a tunnel and set **Webhook URL Override** in the manager to the tunnel's callback URL.

## Polling fallback

Webhooks are the primary mechanism TII uses to inform the application when updates are available. `install_plugins turnitin_tca` also registers a crontab entry that runs the polling command every 20 minutes as a fallback, so any submission that misses its webhook still progresses. The entry is idempotent (keyed on a unique comment) and is skipped where crontab is unavailable or during test runs. You can also run it manually:

```bash
python src/manage.py poll_turnitin_tca
```

## Integration headers

Every request carries `X-Turnitin-Integration-Name: Janeway` and `X-Turnitin-Integration-Version` set to the plugin's `VERSION`. They identify the integration rather than the journal, so they are not configurable.

## Retries and resending

Create Submission and Upload File are tried once when an editor clicks **Send for report**, so the button never hangs. If that attempt fails on a transient error (a network failure, `429` or `5xx`), the file is marked **Retrying** and the polling command sends it again, waiting 10, 20, 40 and then 80 minutes between attempts. A file that was created at Turnitin but failed to upload has only its upload repeated. After five attempts, or on any other error, the file is marked **Error** with the reason.

Failed files show a **Resend** button, and the article page offers **Resend all failed** for every file whose latest send failed. A failed report request is retried automatically by the polling command, up to five attempts, and can always be retried with **Request report again**.

After an outage, resend failed files in bulk (optionally limited to a journal and a start date):

```bash
python src/manage.py resend_failed_turnitin_tca --journal olh --since 2026-09-01
```

Each file is resent as a new submission on behalf of the editor who first sent it.
