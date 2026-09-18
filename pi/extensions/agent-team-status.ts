import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * Small, side-effect-free status command for the first-party package.
 *
 * It deliberately does not call Hermes tools or infer credentials. A future
 * reconciler may bind an explicit service endpoint and capability policy.
 */
export default function agentTeamStatus(pi: ExtensionAPI) {
  pi.registerCommand("agent-team-status", {
    description: "Show where the supervised Agent Team runtime owns status",
    handler: async (_args, ctx) => {
      ctx.ui.notify(
        "Agent Team status is provided by the supervised service; no local Pi runtime state was changed.",
        "info",
      );
    },
  });
}
