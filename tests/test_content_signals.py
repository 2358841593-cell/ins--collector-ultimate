"""Content derivation can use the audited Stage 1 follower count as fallback."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2.content import derive_content_signals  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402


def test_seed_followers_fallback_derives_real_er_and_marks_source():
    cand = {
        "follower_count": None,
        "seed_followers": 10_000,
        "posts": [
            {
                "caption_text": "skincare",
                "media_type": 1,
                "like_count": 100,
                "comment_count": 10,
            },
            {
                "caption_text": "serum",
                "media_type": 2,
                "like_count": 200,
                "comment_count": 20,
            },
        ],
    }
    result = derive_content_signals(cand, load_config())

    assert result["follower_count"] == 10_000
    assert result["follower_count_source"] == "modash_seed_fallback"
    assert result["real_er"] == 1.65
    assert result["real_er_median"] == 1.65


def test_instagram_follower_count_remains_primary():
    cand = {
        "follower_count": 20_000,
        "seed_followers": 10_000,
        "posts": [
            {
                "caption_text": "",
                "media_type": 1,
                "like_count": 200,
                "comment_count": 0,
            }
        ],
    }
    result = derive_content_signals(cand, load_config())

    assert "follower_count" not in result
    assert "follower_count_source" not in result
    assert result["real_er"] == 1.0
