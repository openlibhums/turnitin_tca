"""A thin wrapper around the Turnitin Core API (TCA) REST endpoints.

See https://developers.turnitin.com/turnitin-core-api for the full reference.
All requests carry the three required headers (Authorization plus the two
integration metadata headers) and a short retry loop is applied to absorb
transient failures, in line with Turnitin's retry/polling best practice.
"""

import time
from urllib.parse import quote

import requests


class TurnitinError(Exception):
    """Raised when the TCA returns an unrecoverable error.

    The string form includes any human-readable body Turnitin returned, so the
    reason for a 4xx (e.g. a rejected webhook URL) reaches the editor.
    """

    def __init__(self, message, status_code=None, body=None):
        if body:
            message = "{} - {}".format(message, str(body)[:500])
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    @property
    def transient(self):
        """Whether retrying later could succeed.

        Network failures (no status code), rate limiting and server-side
        errors are transient; any other client error will not improve.
        """
        return (
            self.status_code is None
            or self.status_code == 429
            or self.status_code >= 500
        )

    @property
    def eula_required(self):
        """Turnitin answers 451 when the user has not accepted the EULA."""
        return self.status_code == 451


class TurnitinClient:
    api_version = "v1"
    # (connect, read) timeouts in seconds.
    timeout = (10, 30)
    max_retries = 3
    retry_backoff = 2
    # Upper bound on a server-supplied Retry-After, in seconds.
    max_retry_after = 60

    def __init__(
        self,
        base_url,
        api_key,
        integration_name="Janeway",
        integration_version="1.0",
        timeout=None,
        max_retries=None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.integration_name = integration_name
        self.integration_version = integration_version
        if timeout is not None:
            self.timeout = timeout
        if max_retries is not None:
            self.max_retries = max_retries

    def url(self, path):
        return "{}/api/{}/{}".format(
            self.base_url,
            self.api_version,
            path.lstrip("/"),
        )

    def headers(self, extra=None):
        headers = {
            "Authorization": "Bearer {}".format(self.api_key),
            "X-Turnitin-Integration-Name": self.integration_name,
            "X-Turnitin-Integration-Version": self.integration_version,
        }
        if extra:
            headers.update(extra)
        return headers

    def retry_delay(self, attempt, response=None):
        """Seconds to wait before the next attempt.

        A 429 may carry a Retry-After header, which is honoured up to
        max_retry_after; otherwise exponential backoff applies.
        """
        retry_after = (
            response.headers.get("Retry-After") if response is not None else None
        )
        if retry_after:
            try:
                return min(int(retry_after), self.max_retry_after)
            except ValueError:
                pass
        return self.retry_backoff**attempt

    def request(self, method, path, expected=(200, 201), **kwargs):
        headers = self.headers(kwargs.pop("headers", None))
        url = self.url(path)
        last_exc = None
        for attempt in range(self.max_retries):
            is_last = attempt == self.max_retries - 1
            try:
                response = requests.request(
                    method, url, headers=headers, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                if not is_last:
                    time.sleep(self.retry_delay(attempt))
                continue

            if response.status_code in expected:
                return response
            # Retry rate limiting and server-side failures only; other client
            # errors will not improve.
            error = TurnitinError(
                "TCA {} {} returned {}".format(method, path, response.status_code),
                status_code=response.status_code,
                body=response.text,
            )
            if error.transient and not is_last:
                time.sleep(self.retry_delay(attempt, response))
                continue
            raise error
        raise TurnitinError("TCA request failed: {}".format(last_exc))

    def features_enabled(self):
        return self.request("GET", "features-enabled").json()

    def eula_version_info(self, version="latest"):
        return self.request("GET", "eula/{}".format(version)).json()

    def eula_page(self, version, language="en-US"):
        return self.request(
            "GET",
            "eula/{}/view?lang={}".format(version, language),
            headers={"Accept": "text/html"},
        ).text

    def accept_eula(self, version, user_id, accepted_timestamp, language="en-US"):
        """Record a user's EULA acceptance with Turnitin."""
        return self.request(
            "POST",
            "eula/{}/accept".format(version),
            json={
                "user_id": user_id,
                "accepted_timestamp": accepted_timestamp,
                "language": language,
            },
            headers={"Content-Type": "application/json"},
        ).json()

    def create_submission(self, payload):
        return self.request(
            "POST",
            "submissions",
            expected=(200, 201),
            json=payload,
            headers={"Content-Type": "application/json"},
        ).json()

    def upload_file(self, submission_id, file_bytes, filename):
        disposition = 'inline; filename="{}"'.format(quote(filename))
        return self.request(
            "PUT",
            "submissions/{}/original".format(submission_id),
            expected=(200, 202),
            data=file_bytes,
            headers={
                "Content-Type": "binary/octet-stream",
                "Content-Disposition": disposition,
            },
        )

    def submission_info(self, submission_id):
        return self.request(
            "GET",
            "submissions/{}".format(submission_id),
        ).json()

    def generate_similarity_report(self, submission_id, settings):
        return self.request(
            "PUT",
            "submissions/{}/similarity".format(submission_id),
            expected=(200, 201, 202),
            json=settings,
            headers={"Content-Type": "application/json"},
        )

    def similarity_report_info(self, submission_id):
        return self.request(
            "GET",
            "submissions/{}/similarity".format(submission_id),
        ).json()

    def viewer_url(self, submission_id, payload):
        return self.request(
            "POST",
            "submissions/{}/viewer-url".format(submission_id),
            json=payload,
            headers={"Content-Type": "application/json"},
        ).json()

    def create_webhook(self, payload):
        return self.request(
            "POST",
            "webhooks",
            expected=(200, 201, 202, 204),
            json=payload,
            headers={"Content-Type": "application/json"},
        ).json()

    def list_webhooks(self):
        return self.request("GET", "webhooks").json()

    def delete_webhook(self, webhook_id):
        return self.request(
            "DELETE",
            "webhooks/{}".format(webhook_id),
            expected=(200, 202, 204),
        )
