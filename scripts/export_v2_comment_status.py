"""Shared comment-completion labels for the HTML and XLSX deliverables."""
from __future__ import annotations


def comment_collection_complete(candidate: dict, target_posts: int = 10) -> bool:
    """Return whether delivery may describe comment collection as complete.

    New records carry the explicit deep-collection contract.  Historical
    records predate those fields, but Stage 4's strict gate still accepts them
    when they have the full 10-post window, extracted comments, a real ER, and
    observed post metrics.  Exporters must use that same legacy boundary;
    otherwise a formally accepted row is mislabeled as pending recollection.
    """
    status = candidate.get("deep_collection_status")
    if status == "complete":
        return True
    if status:
        return False
    if candidate.get("comments_read") is not True:
        return False

    posts = [
        post
        for post in (candidate.get("sampled_posts") or [])
        if isinstance(post, dict)
    ]
    try:
        analyzed = int(candidate.get("comments_analyzed") or 0)
        valid = int(candidate.get("valid_comments") or 0)
    except (TypeError, ValueError):
        return False
    has_observed_metric = any(
        post.get("like_count") is not None
        or post.get("comment_count") is not None
        for post in posts
    )
    return (
        len(posts) >= max(1, int(target_posts))
        and analyzed > 0
        and valid >= 20
        and candidate.get("real_er") is not None
        and has_observed_metric
    )
