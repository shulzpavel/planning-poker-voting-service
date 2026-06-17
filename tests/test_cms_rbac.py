from services.voting_service.cms_rbac import (
    ALL_PERMISSION_KEYS,
    CMS_PAGE_DEFINITIONS,
    CMS_PERMISSION_DEFINITIONS,
    hash_password,
    verify_password,
)


def test_password_hash_verification_round_trip():
    encoded = hash_password("correct horse battery staple")

    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_every_cms_page_references_known_permission():
    permission_keys = {permission["key"] for permission in CMS_PERMISSION_DEFINITIONS}

    assert set(ALL_PERMISSION_KEYS) == permission_keys
    assert CMS_PAGE_DEFINITIONS
    for page in CMS_PAGE_DEFINITIONS:
        assert page["permission_key"] in permission_keys
        assert page["path"].startswith("/cms")
