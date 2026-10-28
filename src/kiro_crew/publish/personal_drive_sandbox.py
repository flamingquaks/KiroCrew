"""The personal drive's response-headers policy: the opaque-origin sandbox config.

One source of truth for the policy name, the sandbox CSP and the full policy config, so
the create path and the reuse-verification path in ``kiro_crew.publish.personal_drive``
derive the same values. That module re-exports every name here and stays the import
path callers use.
"""

from __future__ import annotations

from typing import Any

#: Account-global name of the response-headers policy that opaque-origins published
#: artifacts. Account-global because a policy name is unique per account, which is
#: exactly why the drive's find-or-create must VERIFY a same-named policy before reusing
#: it (see ``PersonalDriveProvider._sandbox_headers_policy``).
_SANDBOX_POLICY_NAME = "kirocrew-publish-drive-sandbox"

#: THE sandbox CSP directive -- one source of truth. It is spelled ONCE here so the create
#: path and the reuse-verification path cannot drift: if a second literal existed in the
#: verify path, a future edit to one copy would let the verifier accept (or a create
#: emit) a policy whose CSP does not match, silently defeating the isolation. Delivered
#: as a HEADER (a ``<meta>`` CSP ignores ``sandbox`` by spec), it gives each served
#: document an OPAQUE origin so origin-keyed storage (localStorage/IndexedDB/cookies) is
#: simply unreachable and cannot be shared between mutually-untrusted published documents.
#:
#: ``frame-ancestors 'none'`` rides along in the SAME constant deliberately, rather than
#: living only in the ``FrameOptions`` header below, because this constant is the one thing
#: the reuse path VERIFIES: a policy whose CSP does not match exactly fails closed. Framing
#: protection expressed only as a header would be reused unverified, which is the same
#: fail-open the CSP comparison exists to prevent. It is ``'none'`` and not ``'self'``
#: because every artifact on a drive shares ONE domain -- ``'self'`` would let one published
#: document frame another, which is precisely the cross-document relationship the opaque
#: origin above exists to deny. Nothing legitimately frames a published copy: the
#: dashboard's viewer renders artifact HTML from its own storage, never the public URL.
_SANDBOX_CSP = "sandbox allow-scripts allow-popups; frame-ancestors 'none'"


def _sandbox_policy_config() -> dict[str, Any]:
    """The response-headers-policy config the drive installs -- one source of truth.

    Both the create path and the reuse-verification path derive the required CSP from the
    SAME :data:`_SANDBOX_CSP` constant via this builder, so there is no second literal to
    drift out of sync with the one the create path writes.
    """
    return {
        "Name": _SANDBOX_POLICY_NAME,
        "Comment": "Opaque-origin sandbox for pooled artifact publishing",
        "SecurityHeadersConfig": {
            "ContentSecurityPolicy": {
                "ContentSecurityPolicy": _SANDBOX_CSP,
                "Override": True,
            },
            "ContentTypeOptions": {"Override": True},
            # Legacy companion to the CSP's `frame-ancestors`: a browser too old for CSP3
            # honours only this header, and a published artifact is a public page whose
            # framing a third-party origin would otherwise control. DENY rather than
            # SAMEORIGIN for the reason given on `_SANDBOX_CSP` -- one drive serves every
            # artifact from a single domain, so "same origin" is another published
            # document, not a trusted host.
            "FrameOptions": {"FrameOption": "DENY", "Override": True},
            "StrictTransportSecurity": {
                "AccessControlMaxAgeSec": 31536000,
                "IncludeSubdomains": True,
                "Override": True,
            },
            "ReferrerPolicy": {
                "ReferrerPolicy": "same-origin",
                "Override": True,
            },
        },
    }
