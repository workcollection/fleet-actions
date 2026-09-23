# Security policy

This repository is public and its workflows run inside the CI of many other
repositories, so a defect here has a wide blast radius. Reports are welcome.

## Reporting a vulnerability

1. **Preferred:** open a private report on GitHub —
   <https://github.com/workcollection/fleet-actions/security/advisories/new>
   (private vulnerability reporting is enabled for this repository).
2. **E-mail:** security@catboy.systems. If the report contains exploit details,
   ask for an encryption key in a private report first.

Please do not open a public issue for anything you believe is a security problem.

## Scope

The composite actions and reusable workflows in this repository and the way they
handle caller inputs, runner selection and downloaded tools. Third-party actions
they call are in scope only where this repository's pinning or usage is the problem;
report the action itself upstream as well.

## Supported versions

| Version | Supported |
|---|---|
| latest release tag (`v2`) | yes |
| `main` | yes (best effort) |
| older tags (`v1`) | security fixes only until every consumer has moved |

## What to expect

- Acknowledgement within **72 hours**.
- A fix or mitigation targeted within **30 days** of confirmation; you will be told
  if it takes longer and why.
- Coordinated disclosure: we ask you to hold details until a fix is released.
  Credit in the release notes on request.
