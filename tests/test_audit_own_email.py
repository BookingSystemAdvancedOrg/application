"""Audit #35: a caller cannot change their own login email through
PUT /users/<own sub>. A stolen access token must not become a permanent
takeover via a new, already verified address. An owner still changes a
staff user's email (vouched for, stays verified)."""

from test_manage_user import (CALLER_SUB, TARGET_SUB, USER_POOL_ID, app_state, assert_response,  # noqa: F401
                              get_user, make_event, put_user, response_body, user_item)


def test_owner_cannot_change_their_own_email(app_state):
    app, user_table, cognito = app_state
    put_user(user_table, user_item(sub=CALLER_SUB, role="owner_user", location_id="", email="owner@example.com"))

    response = app.handler(
        make_event(method="PUT", proxy=CALLER_SUB, groups='["owner_user"]',
                   body={"email": "attacker@example.com", "name": "Owner"}),
        None,
    )

    assert_response(response, 403)
    body = response_body(response)
    assert body["error"] == "forbidden" and body["field"] == "email" and body["reason"] == "own_email_locked"
    assert get_user(user_table, CALLER_SUB)["email"] == "owner@example.com"
    assert get_user(user_table, CALLER_SUB)["name"] == "Test User"
    cognito.admin_update_user_attributes.assert_not_called()


def test_staff_cannot_change_their_own_email(app_state):
    app, user_table, cognito = app_state
    put_user(user_table, user_item(sub=CALLER_SUB, role="staff", location_id="location-id"))

    response = app.handler(
        make_event(method="PUT", proxy=CALLER_SUB, groups='["staff_user"]', body={"email": "x@example.com"}),
        None,
    )

    assert_response(response, 403)
    cognito.admin_update_user_attributes.assert_not_called()


def test_own_profile_with_the_same_email_still_saves(app_state):
    app, user_table, cognito = app_state
    put_user(user_table, user_item(sub=CALLER_SUB, role="owner_user", location_id="", email="owner@example.com"))

    response = app.handler(
        make_event(method="PUT", proxy=CALLER_SUB, groups='["owner_user"]',
                   body={"email": "owner@example.com", "name": "Owner New"}),
        None,
    )

    assert_response(response, 200)
    assert get_user(user_table, CALLER_SUB)["name"] == "Owner New"
    cognito.admin_update_user_attributes.assert_called_once_with(
        UserPoolId=USER_POOL_ID, Username=CALLER_SUB, UserAttributes=[{"Name": "name", "Value": "Owner New"}])


def test_owner_still_changes_a_staff_users_email(app_state):
    app, user_table, cognito = app_state
    put_user(user_table, user_item())
    cognito.admin_get_user.return_value = {
        "Enabled": True,
        "UserAttributes": [{"Name": "email", "Value": "test.user@example.com"},
                           {"Name": "email_verified", "Value": "true"}],
    }

    response = app.handler(make_event(method="PUT", proxy=TARGET_SUB, body={"email": "new@example.com"}), None)

    assert_response(response, 200)
    assert get_user(user_table)["email"] == "new@example.com"
