# Hermes integration adapter

This optional first-party plugin registers the session-aware
`delegate_to_agent_team` tool and translates an authored brief plus caller
session context into the Agent Team HTTP contract. Agent Team does not depend on
the caller's identity or user-facing routing model.

Install or update it explicitly from a trusted Agent Team revision:

```sh
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
cp -R integrations/hermes/principal-agent-team \
  "${HERMES_HOME:-$HOME/.hermes}/plugins/principal-agent-team"
hermes gateway restart
```

Set `AGENT_TEAM_URL` only when the service is not available at the documented
loopback default. If optional Agent Team HTTP authentication is enabled, the
gateway process must receive `AGENT_TEAM_API_TOKEN`; the plugin forwards that
control credential without serializing it into plugin configuration.

This plugin is part of the AGPL-3.0-only Agent Team runtime; see the repository
root `LICENSE`.
