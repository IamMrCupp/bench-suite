<!--
Title: Conventional Commits, under 70 chars. e.g. feat(services): set one password on every service
-->

Closes #

## Summary

<!-- What changed and why. Name the tradeoff if there was one. -->

## Test plan

<!--
Required for anything that changes what bench-setup does to a host. Not "CI
passes" — numbered, copy-pasteable steps with concrete expected outcomes.
Pure-docs or pure-CI changes: a one-line "no runtime change; verification is CI
alone" is fine, but say it explicitly.
-->

- [ ] **1.**
  ```bash
  ```
  Expected:

- [ ] **2. CI green**

## Secrets and host-safety check

<!--
bench-setup handles the shared password and every service's API token, and it
runs as root. Delete this section only if the change can't touch any of that.
-->

- [ ] No password or token on a command line, in `ps`, in a log or in output (stdin only)
- [ ] Files holding a secret are written owner-only
- [ ] A re-run changes nothing that's already set up
- [ ] Nothing new listens beyond 127.0.0.1, and bench-recon stays off unless the owner turns it on
