import json

from extensions.sop_v2.pipeline import modash_cdp


class _FakePage:
    def __init__(self):
        self.exact_queries = []
        self.waits = []
        self.bulk_calls = 0

    def evaluate(self, expression, payload):
        if expression == modash_cdp._EXACT_SEARCH_JS:
            self.exact_queries.append(payload)
            if payload == "exact_user":
                return json.dumps(
                    {
                        "results": [
                            {
                                "username": "near_exact_user",
                                "serviceSdId": "wrong-id",
                            },
                            {
                                "username": "EXACT_USER",
                                "serviceSdId": "current-exact-id",
                            },
                        ]
                    }
                )
            return json.dumps(
                {
                    "results": [
                        {
                            "username": "different_user",
                            "serviceSdId": "must-not-map",
                        }
                    ]
                }
            )

        assert expression == modash_cdp._SEARCH_JS
        self.bulk_calls += 1
        if self.bulk_calls == 1:
            return json.dumps(
                {
                    "results": [
                        {
                            "username": "Batch_User",
                            "serviceSdId": "current-bulk-id",
                        },
                        {
                            "username": "legacy_user",
                            "servicePlatformId": "legacy-bulk-id",
                        },
                    ]
                }
            )
        return json.dumps({"results": []})

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


def test_resolve_platform_ids_supports_current_ids_and_exact_handle_fallback():
    page = _FakePage()

    result = modash_cdp.resolve_platform_ids(
        page,
        ["@batch_user", "legacy_user", "exact_user", "absent_user"],
        "skincare",
        {"followers": {"min": 1000}},
        max_pages=2,
    )

    assert result == {
        "batch_user": "current-bulk-id",
        "legacy_user": "legacy-bulk-id",
        "exact_user": "current-exact-id",
    }
    assert page.exact_queries.count("absent_user") == 3
    assert page.exact_queries.count("batch_user") == 3
    assert page.exact_queries.count("legacy_user") == 3
    assert page.exact_queries.count("exact_user") == 1
    assert 100 in page.waits
