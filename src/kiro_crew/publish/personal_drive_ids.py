"""The personal drive's publish handle: the ``external_id`` format and its one parser.

The handle binds an unguessable object key to the AWS account that holds the copy, so
every later operation on a publication runs against that account rather than whatever
account is the registry default at the time. ``kiro_crew.publish.personal_drive``
re-exports every name here and stays the import path callers use.
"""

from __future__ import annotations

import re
import secrets

#: ``external_id`` is ``<random>~<profile>``. The RANDOM half comes first so a profile
#: name containing the separator cannot corrupt the object key, and the key/URL use only
#: that half -- an account label never reaches a public link.
_ID_SEP = "~"
_EXTERNAL_ID_BYTES = 16

#: An AWS account id is exactly twelve digits and cannot contain :data:`_ID_SEP`. That is
#: what makes the three-field handle unambiguous even though a PROFILE NAME may contain the
#: separator: the middle field is read as an account ONLY if it has this shape, so a
#: two-field handle minted before binding existed cannot have its profile name mistaken for
#: an account.
_ACCOUNT_RE = re.compile(r"^\d{12}$")


def make_external_id(profile: str, account: str) -> str:
    """A publish handle binding an unguessable key to the ACCOUNT that holds the copy.

    Shape: ``<key>~<account>~<profile>``. The account is what the withdrawal paths verify;
    the profile name is retained only so the AWS calls have a credential source to resolve.

    Binding the NAME alone was the earlier shape and it was a real exposure, not a nicety.
    A profile name is a local alias the owner can repoint at another account by editing
    their AWS config. Do that, then delete the artifact, and the withdrawal resolved the
    name to the NEW account, found no drive there, reported a removal that removed nothing,
    and cleared the record -- leaving the original copy public in the old account with its
    handle erased. That needed no race and no failure, just a config edit and an ordinary
    delete, which is why it was fixed here rather than deferred with the timing-dependent
    windows.
    """
    key = secrets.token_hex(_EXTERNAL_ID_BYTES)
    return f"{key}{_ID_SEP}{account}{_ID_SEP}{profile}"


def _parse_external_id(external_id: str) -> tuple[str, str, str]:
    """Return ``(key_part, bound_account, bound_profile)``; unknown fields are ``''``.

    The single parser for this format, so the readers built on it cannot drift apart.
    Three shapes are accepted, and the middle one is the reason for :data:`_ACCOUNT_RE`:

    * ``key~account~profile`` -- current. Account and profile both known.
    * ``key~profile`` -- minted before binding, or hand-written by a caller. The middle
      field fails the account shape, so it is read as the PROFILE and the account is
      unknown. Reading it as an account would be the worst possible error: the withdrawal
      paths would then compare a profile name against a live account id, mismatch every
      time, and refuse every operation on every pre-existing publication.
    * ``key`` -- no separator at all. Both unknown; the caller falls back to the registry
      default, which is the pre-binding behaviour.
    """
    key_part, _, rest = (external_id or "").partition(_ID_SEP)
    if not rest:
        return key_part, "", ""
    head, sep, tail = rest.partition(_ID_SEP)
    if sep and _ACCOUNT_RE.match(head):
        return key_part, head, tail
    return key_part, "", rest


def split_external_id(external_id: str) -> tuple[str, str]:
    """Return ``(key_part, bound_profile)``; the profile is ``''`` when unbound.

    Kept at two fields because that is all its callers need -- they want the object key,
    or the credential source. The account is read through :func:`bound_account` by the one
    place that verifies it.
    """
    key_part, _account, bound = _parse_external_id(external_id)
    return key_part, bound


def bound_account(external_id: str) -> str:
    """The account id pinned into the handle, or ``''`` when the handle predates binding."""
    _key, account, _profile = _parse_external_id(external_id)
    return account
