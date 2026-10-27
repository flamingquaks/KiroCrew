"""An ARN's resource id survives the bare-secret pass; a key beside or posing as one does not."""

from __future__ import annotations

import pytest

from kiro_crew.security import redact_credentials
from kiro_crew.security import redaction as _redaction

# The canonical AWS documentation example secret key, never a live credential.
KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"

# The issue's shape: a mixed-case, digit-bearing target-group name glued by `/`
# to its hex id, which is one run the heuristic judges a key on its own.
TARGET_GROUP_RESOURCE = "targetgroup/MyProdWebAppTargetGroupV2/73e2d6bc24d8a067"
TARGET_GROUP_ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:" + TARGET_GROUP_RESOURCE


def test_the_fixtures_are_what_they_claim() -> None:
    assert len(KEY) == 40
    assert _redaction._looks_like_secret_key(KEY)
    assert _redaction._text_contains_bare_secret(TARGET_GROUP_RESOURCE)


def test_without_the_exemption_the_target_group_arn_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_redaction, "_arn_resource_spans", lambda text: [])
    cleaned, warnings = redact_credentials(TARGET_GROUP_ARN)
    assert TAG in cleaned
    assert warnings


@pytest.mark.parametrize(
    "template",
    [
        "{}",
        '"TargetGroupArn": "{}"',
        "--target-group-arn={}",
        "TargetGroupArn: {}",
        "<{}>",
        "({})",
    ],
)
def test_the_target_group_arn_is_left_intact_with_no_warning(template: str) -> None:
    text = template.format(TARGET_GROUP_ARN)
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws-us-gov:elasticloadbalancing:us-gov-west-1:123456789012:" + TARGET_GROUP_RESOURCE,
        "arn:aws-cn:elasticloadbalancing:cn-north-1:123456789012:" + TARGET_GROUP_RESOURCE,
    ],
)
def test_other_partitions_are_exempt(arn: str) -> None:
    assert redact_credentials(arn) == (arn, [])


@pytest.mark.parametrize(
    "text",
    [
        f"{TARGET_GROUP_ARN} {KEY}",
        f"{KEY} {TARGET_GROUP_ARN}",
        f'"{TARGET_GROUP_ARN}","{KEY}"',
        f"{TARGET_GROUP_ARN},{KEY}",
        f"{TARGET_GROUP_ARN}={KEY}",
    ],
)
def test_a_bare_key_on_the_same_line_is_still_redacted(text: str) -> None:
    cleaned, warnings = redact_credentials(text)
    assert KEY not in cleaned
    assert TARGET_GROUP_ARN in cleaned
    assert warnings == ["Redacted bare secret key (40 chars)"]


@pytest.mark.parametrize(
    "prefix",
    [
        "myarn:aws:elasticloadbalancing:us-east-1:123456789012:",
        "xarn:aws:s3:us-east-1:123456789012:",
        "arn:aws:elasticloadbalancing:us-east-1:12345678901:",
        "arn:aws:elasticloadbalancing:us-east-1:1234567890123:",
        "arn:aws:s3:::",
        "arn:notaws:elasticloadbalancing:us-east-1:123456789012:",
        "ARN:AWS:elasticloadbalancing:us-east-1:123456789012:",
        # A real ARN whose whole resource is one key: the run IS a key.
        "arn:aws:elasticloadbalancing:us-east-1:123456789012:",
    ],
)
def test_a_key_after_a_lookalike_prefix_is_still_redacted(prefix: str) -> None:
    cleaned, warnings = redact_credentials(prefix + KEY)
    assert KEY not in cleaned
    assert cleaned.endswith(TAG)
    assert warnings == ["Redacted bare secret key (40 chars)"]


def test_a_key_after_a_lookalike_prefix_beside_a_real_arn_is_still_redacted() -> None:
    text = f"{TARGET_GROUP_ARN} myarn:aws:elasticloadbalancing:us-east-1:123456789012:{KEY}"
    cleaned, _ = redact_credentials(text)
    assert KEY not in cleaned
    assert TARGET_GROUP_ARN in cleaned


@pytest.mark.parametrize(
    "text",
    [
        # Glued into the resource of a real ARN that is not a target group.
        f"arn:aws:iam::123456789012:user/{KEY}",
        f"arn:aws:secretsmanager:us-east-1:123456789012:secret/{KEY}",
        f"arn:aws:lambda:us-east-1:123456789012:function:fn/{KEY}",
        # Glued after a real target-group id: the shape is closed at the id.
        f"{TARGET_GROUP_ARN}/{KEY}",
        f"{TARGET_GROUP_ARN}{KEY}",
        # A target-group shape that breaks the service's rules.
        f"arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/{KEY}/73e2d6bc24d8a067",
        f"arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/x/{KEY}",
    ],
)
def test_a_key_glued_into_any_other_arn_resource_is_still_redacted(text: str) -> None:
    # Only the closed target-group shape is exempt, so every place a key can be
    # glued into an ARN is judged exactly as on main.
    cleaned, warnings = redact_credentials(text)
    assert KEY not in cleaned
    assert warnings


def test_the_exemption_matches_only_the_target_group_resource() -> None:
    assert _redaction._arn_resource_spans(f"x {TARGET_GROUP_ARN} y") == [
        (2 + TARGET_GROUP_ARN.index("targetgroup/"), 2 + len(TARGET_GROUP_ARN))
    ]
    for other in (
        "arn:aws:iam::123456789012:role/" + TARGET_GROUP_RESOURCE,
        "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/x/50dc6c495c0c9188",
        f"{TARGET_GROUP_ARN}0",
    ):
        assert _redaction._arn_resource_spans(other) == [], other


def test_labelled_and_fixed_shape_credentials_inside_an_arn_are_still_redacted() -> None:
    # Passes 1 and 2 do not consult the ARN shape.
    text = "arn:aws:iam::123456789012:user/AKIAIOSFODNN7EXAMPLE"
    cleaned, warnings = redact_credentials(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
    assert warnings


def test_the_exemption_leaves_the_exfil_scan_unchanged() -> None:
    assert _redaction._text_contains_bare_secret(TARGET_GROUP_ARN)


def test_many_arns_are_scanned_once() -> None:
    calls = 0
    real = _redaction._arn_resource_spans

    def counting(text: str) -> list[tuple[int, int]]:
        nonlocal calls
        calls += 1
        return real(text)

    text = "\n".join([TARGET_GROUP_ARN] * 500)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_redaction, "_arn_resource_spans", counting)
        assert redact_credentials(text) == (text, [])
    assert calls == 1
