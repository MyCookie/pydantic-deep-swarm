# Issue and specification lookup

The issue tracker is GitHub repository `MyCookie/pydantic-deep-swarm`.
An explicit issue in the request is authoritative for that feature; read its
body and relevant comments before implementing or reviewing:

```sh
gh issue view ISSUE_NUMBER --repo MyCookie/pydantic-deep-swarm --comments
```

If an issue is absent, use the user's agreed specification and link the retained
repository specification from the PR. Creating an issue requires authorization;
lookup and review do not imply permission to post comments or change issues.
The current GitHub workflow change is specified in
[github-workflow-spec.md](github-workflow-spec.md).

Existing runtime contracts are indexed in [docs/README.md](../README.md). Record
the issue/specification revision and the fixed review baseline. Reviews compare
the accumulated diff against that baseline; current Actions reports establish
implementation status, while old issue comments establish historical decisions.
