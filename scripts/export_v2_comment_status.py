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


def comment_unavailable_note(candidate: dict) -> str:
    """Return a transparent delivery label for reviewed unavailable threads."""
    rows = [
        row
        for row in (candidate.get("comment_unavailable_posts") or [])
        if isinstance(row, dict)
    ]
    if not rows:
        return ""
    empty_thread = sum(
        row.get("reason") == "verified_empty_thread_despite_reported_count"
        for row in rows
    )
    low_retry = sum(
        row.get("reason") == "reported_low_count_unavailable_after_retry"
        for row in rows
    )
    other = len(rows) - empty_thread - low_retry
    labels = []
    if empty_thread:
        labels.append(
            f"{empty_thread}帖评论线程明确为空"
            "（页面与端点双重核验；原上报数保留）"
        )
    if low_retry:
        labels.append(f"{low_retry}帖低量评论重复不可见（已复采）")
    if other:
        labels.append(f"{other}帖评论不可见（证据已保留）")
    return "；".join(labels)
