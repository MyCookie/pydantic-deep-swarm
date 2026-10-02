# Agent Team

Agent Team coordinates user requests, specialist work, and durable knowledge
across runs.

## Language

**State directory**:
The external location for one Agent Team runtime's durable state and recovery
evidence.

**Workspace directory**:
The external location for managed project files and artifacts, selected
independently from the state directory.

**Model selection**:
The operator's choice of a named model or automatic model selection for a role.

**Desired configuration**:
The configuration selected for the next runtime activation. Persistence alone
does not prove that an existing runtime has activated it.

**Runtime generation**:
One interval of exclusive runtime ownership, distinguishing its work from work
left behind by a predecessor.

**Shared knowledge**:
Durable findings available to Agent Team roles across runs and projects.
_Avoid_: Conversation history, agent memory

**Knowledge record**:
A finding in shared knowledge, together with its identity, provenance, and
relationship to any finding that replaces it.

**Knowledge store**:
The authoritative collection of shared knowledge for one Agent Team state
directory.
_Avoid_: Knowledge cache

**Historical knowledge**:
All retained knowledge records, including records replaced by later findings
and their original provenance and replacement relationships.
_Avoid_: Visible knowledge
