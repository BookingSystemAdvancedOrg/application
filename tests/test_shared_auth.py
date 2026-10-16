import pytest

from shared.auth import Unauthorized, get_groups, require_group


def make_event(groups):
    return {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "caller-sub",
                        "cognito:groups": groups,
                    }
                }
            }
        }
    }


@pytest.mark.parametrize(
    ("raw_groups", "expected"),
    [
        (["owner_user", "super_user"], ["owner_user", "super_user"]),
        ('["owner_user", "super_user"]', ["owner_user", "super_user"]),
        ("[owner_user, super_user]", ["owner_user", "super_user"]),
        ('"owner_user"', ["owner_user"]),
        ("owner_user", ["owner_user"]),
        ("", []),
        (None, []),
    ],
)
def test_get_groups_normalizes_api_gateway_claims(raw_groups, expected):
    assert get_groups(make_event(raw_groups)) == expected


def test_require_group_accepts_an_allowed_group():
    require_group(make_event('["owner_user"]'), "owner_user", "super_user")


def test_require_group_rejects_other_groups():
    with pytest.raises(Unauthorized):
        require_group(
            make_event('["staff_user"]'),
            "owner_user",
            "super_user",
        )
