# Hermes Principal integration

This first-party Hermes plugin registers the session-aware
`delegate_to_agent_team` tool. It keeps Hermes's normal AIAgent as the
user-facing Principal and sends typed briefs directly to Agent Team.

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
