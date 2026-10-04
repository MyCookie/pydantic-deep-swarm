"""Principal → Manager → Worker, with typed handoffs and no model calls."""

import argparse

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, field_validator

from agent_team.contracts import (
    ProjectBrief, Requirement, RequirementResult, WorkerResult, WorkerTaskSpec,
)


class UserInput(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def require_words(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("Text must contain at least one word")
        return text


class ManagerInput(BaseModel):
    brief: ProjectBrief


class WorkerInput(BaseModel):
    task: WorkerTaskSpec


class SwarmState(UserInput):
    brief: ProjectBrief | None = None
    task: WorkerTaskSpec | None = None
    result: WorkerResult | None = None


def principal(state: UserInput) -> dict:
    return {"brief": ProjectBrief(
        objective="Count the words in the supplied text",
        requirements=[Requirement(
            id="word-count", description="Count whitespace-separated words",
        )],
        relevant_context=[state.text],
        desired_output="A word count with evidence",
    )}


def manager(state: ManagerInput) -> dict:
    return {"task": WorkerTaskSpec(
        task_id="count-words", role="word-counter",
        objective=state.brief.objective,
        requirement_ids=[item.id for item in state.brief.requirements],
        relevant_context=state.brief.relevant_context,
        deliverable=state.brief.desired_output,
        tools=[],
    )}


def worker(state: WorkerInput) -> dict:
    count = len(state.task.relevant_context[0].split())
    evidence = [f"Counted {count} whitespace-separated words."]
    return {"result": WorkerResult(
        worker_id="example-worker", task_id=state.task.task_id, role=state.task.role,
        status="complete", summary=f"Word count: {count}", evidence=evidence,
        requirement_results=[RequirementResult(
            requirement_id=requirement_id, status="satisfied", evidence=evidence,
        ) for requirement_id in state.task.requirement_ids],
    )}


# Node schemas restrict read channels; they do not provide process confinement.
builder = StateGraph(SwarmState, input_schema=UserInput)
builder.add_node("principal", principal, input_schema=UserInput)
builder.add_node("manager", manager, input_schema=ManagerInput)
builder.add_node("worker", worker, input_schema=WorkerInput)
builder.add_edge(START, "principal")
builder.add_edge("principal", "manager")
builder.add_edge("manager", "worker")
builder.add_edge("worker", END)
graph = builder.compile()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help="Quoted text; words are separated by whitespace")
    output = graph.invoke({"text": parser.parse_args().text})
    print(output["result"].model_dump_json(indent=2))
