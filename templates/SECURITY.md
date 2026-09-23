# Security policy

<!--
Fleet template (workcollection/fleet-actions/templates/SECURITY.md). Copy to the repo
root as SECURITY.md and replace the <PLACEHOLDERS>. Repos under an organisation that
ships a default SECURITY.md in its `.github` repository inherit it and do not need a
copy; repos under a user account do (user accounts have no org default).
-->

## Reporting a vulnerability

1. **Preferred:** open a private report on GitHub —
   <https://github.com/<OWNER>/<REPO>/security/advisories/new>
   (private vulnerability reporting is enabled for this repository).
2. **E-mail:** <SECURITY_MAILBOX> — encrypt with the key published at
   <PGP_KEY_URL> if the report contains exploit details.

Please do not open a public issue for anything you believe is a security problem.

## Scope

This policy covers the code in this repository and the artifacts it publishes
(container images, packages, releases). Third-party dependencies are in scope only
where this project's use of them is the problem; report the dependency itself
upstream as well.

## Supported versions

| Version | Supported |
|---|---|
| latest release | yes |
| `main` | yes (best effort) |
| older releases | no |

## What to expect

- Acknowledgement within **72 hours**.
- A fix or mitigation targeted within **30 days** of confirmation; you will be told
  if it takes longer and why.
- Coordinated disclosure: we ask you to hold details until a fix is released.
  Credit in the release notes on request.
