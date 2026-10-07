from django.urls import reverse

from plugins.turnitin_tca import logic
from utils.logger import get_logger

logger = get_logger(__name__)


def review_action_hook(context):
    """Render a link into the editor Actions block on Unassigned / Review."""
    request = context.get("request")
    article = context.get("article")
    if not request or not article:
        return ""
    try:
        if not logic.is_enabled(request.journal):
            return ""
    except Exception as exc:  # pragma: no cover - defensive, never break the page
        logger.error("Turnitin hook error: %s", exc)
        return ""

    url = reverse("turnitin_tca_article", kwargs={"article_id": article.pk})
    return (
        '<li><a href="{url}"><i class="fa fa-check-circle"></i> '
        "Turnitin Similarity</a></li>"
    ).format(url=url)
