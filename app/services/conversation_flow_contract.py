from dataclasses import dataclass
import re
from string import Formatter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetOption(StrictModel):
    id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    action: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    next_node: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @model_validator(mode="after")
    def one_target(self):
        if (self.action is None) == (self.next_node is None):
            raise ValueError("debe definir exactamente action o next_node")
        return self


class ReplyButtonOption(TargetOption):
    title: str = Field(min_length=1, max_length=20)


class ListRowOption(TargetOption):
    title: str = Field(min_length=1, max_length=24)
    description: str | None = Field(default=None, max_length=72)


class TextNode(StrictModel):
    id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    type: Literal["text"]
    body: str = Field(min_length=1, max_length=4096)
    terminal: Literal[True] = True


class ReplyButtonNode(StrictModel):
    id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    type: Literal["reply_button"]
    body: str = Field(min_length=1, max_length=1024)
    header: str | None = Field(default=None, max_length=60)
    footer: str | None = Field(default=None, max_length=60)
    terminal: Literal[False] = False
    options: list[ReplyButtonOption] = Field(min_length=1, max_length=3)


class ListSection(StrictModel):
    title: str | None = Field(default=None, max_length=24)
    options: list[ListRowOption] = Field(min_length=1, max_length=10)


class ListNode(StrictModel):
    id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    type: Literal["list"]
    body: str = Field(min_length=1, max_length=1024)
    button: str = Field(min_length=1, max_length=20)
    header: str | None = Field(default=None, max_length=60)
    footer: str | None = Field(default=None, max_length=60)
    terminal: Literal[False] = False
    sections: list[ListSection] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def at_most_ten_rows(self):
        if sum(len(section.options) for section in self.sections) > 10:
            raise ValueError("una lista admite como maximo 10 filas")
        if len(self.sections) > 1 and any(
            not section.title for section in self.sections
        ):
            raise ValueError(
                "todas las secciones necesitan titulo cuando hay mas de una"
            )
        return self


FlowNode = Annotated[
    TextNode | ReplyButtonNode | ListNode,
    Field(discriminator="type"),
]


class FlowDefinition(StrictModel):
    start_node: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    nodes: list[FlowNode] = Field(min_length=1, max_length=100)


class CreateConversationFlowRequest(StrictModel):
    slug: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    name: str = Field(min_length=1, max_length=120)
    event_key: str = Field(min_length=1, max_length=120)
    definition: dict[str, Any]


class SaveConversationFlowDraftRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    definition: dict[str, Any]


class ValidateConversationFlowRequest(StrictModel):
    event_key: str = Field(min_length=1, max_length=120)
    definition: dict[str, Any]


@dataclass(frozen=True)
class EventPolicy:
    variables: frozenset[str] = frozenset()
    actions: frozenset[str] = frozenset()
    terminal_only: bool = False


EVENT_POLICIES: dict[str, EventPolicy] = {
    "onboarding.invitation": EventPolicy(
        frozenset({"registration_url", "ttl_minutes"}),
        terminal_only=True,
    ),
    "onboarding.error": EventPolicy(terminal_only=True),
    "dashboard.link.sent": EventPolicy(
        frozenset({"login_url", "ttl_minutes"}),
        terminal_only=True,
    ),
    "dashboard.link.not_eligible": EventPolicy(terminal_only=True),
    "dashboard.link.error": EventPolicy(terminal_only=True),
    "movement.registered": EventPolicy(
        frozenset({"movement_type", "description", "amount", "currency"}),
        terminal_only=True,
    ),
    "movement.invalid_data": EventPolicy(
        frozenset({"reason"}),
        terminal_only=True,
    ),
    "movement.persistence_error": EventPolicy(terminal_only=True),
    "movement.updated": EventPolicy(
        frozenset({"description", "amount", "currency", "category"}),
        terminal_only=True,
    ),
    "movement.annulled": EventPolicy(
        frozenset({"description", "amount", "currency"}),
        terminal_only=True,
    ),
    "movement.category_hint": EventPolicy(
        actions=frozenset({"request_category_change"})
    ),
    "category.confirmation_required": EventPolicy(
        frozenset({"category"}),
        frozenset(
            {"confirm_category", "reject_category", "cancel_pending_operation"}
        ),
    ),
    "category.deleted": EventPolicy(frozenset({"category"})),
    "category.not_found": EventPolicy(frozenset({"category"})),
    "budget.compensation_proposed": EventPolicy(
        frozenset({"summary"}),
        frozenset(
            {
                "confirm_compensation",
                "reject_compensation",
                "cancel_pending_operation",
            }
        ),
    ),
    "reminder.missing_concept": EventPolicy(
        actions=frozenset({"cancel_pending_operation"})
    ),
    "reminder.missing_day": EventPolicy(
        frozenset({"concept"}),
        frozenset({"cancel_pending_operation"}),
    ),
    "reminder.created": EventPolicy(
        frozenset({"title", "day", "amount", "currency"})
    ),
    "reminder.duplicate": EventPolicy(frozenset({"title"})),
    "reminder.updated": EventPolicy(frozenset({"title"})),
    "reminder.paused": EventPolicy(frozenset({"title"})),
    "reminder.activated": EventPolicy(frozenset({"title"})),
    "reminder.deleted": EventPolicy(frozenset({"title"})),
    "limit.missing_data": EventPolicy(
        frozenset({"missing_field"}),
        frozenset({"cancel_pending_operation"}),
    ),
    "limit.year_confirmation": EventPolicy(
        frozenset({"year"}),
        frozenset(
            {"confirm_limit_year", "reject_limit", "cancel_pending_operation"}
        ),
    ),
    "limit.category_confirmation": EventPolicy(
        frozenset({"category"}),
        frozenset(
            {"confirm_limit_category", "reject_limit", "cancel_pending_operation"}
        ),
    ),
    "limit.created": EventPolicy(
        frozenset({"category", "amount", "currency", "period"})
    ),
    "limit.updated": EventPolicy(
        frozenset({"category", "amount", "currency", "period"})
    ),
    "limit.listed": EventPolicy(frozenset({"summary"})),
    "limit.deleted": EventPolicy(frozenset({"category", "period"})),
    "limit.bulk_deleted": EventPolicy(
        frozenset({"category", "periods", "count"}),
        terminal_only=True,
    ),
    "limit.month_selection": EventPolicy(
        frozenset({"year"}),
        frozenset({"cancel_pending_operation"}),
    ),
    "budget.result": EventPolicy(frozenset({"summary"})),
    "movements.query_result": EventPolicy(frozenset({"summary"})),
    "conversation.context_lost": EventPolicy(),
    "conversation.cancelled": EventPolicy(),
}


@dataclass(frozen=True)
class FlowValidationIssue:
    path: str
    message: str


class ConversationFlowDefinitionInvalid(Exception):
    def __init__(self, issues: list[FlowValidationIssue]):
        super().__init__("La definicion del recorrido es invalida.")
        self.issues = tuple(issues)


def validate_flow_definition(
    event_key: str,
    raw_definition: dict[str, Any],
) -> dict[str, Any]:
    policy = EVENT_POLICIES.get(event_key)
    if policy is None:
        raise ConversationFlowDefinitionInvalid(
            [FlowValidationIssue("event_key", "el evento no esta habilitado por el backend")]
        )

    try:
        definition = FlowDefinition.model_validate(raw_definition)
    except ValidationError as exc:
        raise ConversationFlowDefinitionInvalid(
            [
                FlowValidationIssue(
                    ".".join(str(part) for part in error["loc"]),
                    error["msg"],
                )
                for error in exc.errors()
            ]
        ) from exc

    issues = _semantic_issues(definition, policy)
    if issues:
        raise ConversationFlowDefinitionInvalid(issues)
    return definition.model_dump(mode="json", exclude_none=True)


def available_contract() -> dict[str, Any]:
    return {
        "events": [
            {
                "event_key": event_key,
                "variables": sorted(policy.variables),
                "actions": sorted(policy.actions),
                "terminal_only": policy.terminal_only,
            }
            for event_key, policy in sorted(EVENT_POLICIES.items())
        ],
        "node_types": ["text", "reply_button", "list"],
    }


def _semantic_issues(
    definition: FlowDefinition,
    policy: EventPolicy,
) -> list[FlowValidationIssue]:
    issues: list[FlowValidationIssue] = []
    nodes_by_id: dict[str, FlowNode] = {}
    option_ids: set[str] = set()

    if policy.terminal_only and (
        len(definition.nodes) != 1
        or not isinstance(definition.nodes[0], TextNode)
    ):
        issues.append(
            FlowValidationIssue(
                "nodes",
                "el evento es terminal y solo admite un nodo de texto",
            )
        )

    for node_index, node in enumerate(definition.nodes):
        if node.id in nodes_by_id:
            issues.append(
                FlowValidationIssue(
                    f"nodes.{node_index}.id", "el identificador de nodo esta repetido"
                )
            )
        nodes_by_id[node.id] = node
        for option_index, option in enumerate(_options(node)):
            if option.id in option_ids:
                issues.append(
                    FlowValidationIssue(
                        f"nodes.{node_index}.options.{option_index}.id",
                        "el identificador de opcion esta repetido",
                    )
                )
            option_ids.add(option.id)
            if option.action and option.action not in policy.actions:
                issues.append(
                    FlowValidationIssue(
                        f"nodes.{node_index}.options.{option_index}.action",
                        "la accion no esta permitida para el evento",
                    )
                )

    if definition.start_node not in nodes_by_id:
        issues.append(FlowValidationIssue("start_node", "el nodo inicial no existe"))

    for node_index, node in enumerate(definition.nodes):
        for option_index, option in enumerate(_options(node)):
            if option.next_node and option.next_node not in nodes_by_id:
                issues.append(
                    FlowValidationIssue(
                        f"nodes.{node_index}.options.{option_index}.next_node",
                        "el nodo de destino no existe",
                    )
                )

    issues.extend(_template_issues(definition, policy))
    if issues or definition.start_node not in nodes_by_id:
        return issues

    reachable = _reachable_nodes(definition.start_node, nodes_by_id)
    for node_index, node in enumerate(definition.nodes):
        if node.id not in reachable:
            issues.append(
                FlowValidationIssue(
                    f"nodes.{node_index}.id", "el nodo no es alcanzable desde start_node"
                )
            )

    termination_cache: dict[str, bool] = {}
    for node_id in reachable:
        if not _all_paths_terminate(node_id, nodes_by_id, set(), termination_cache):
            issues.append(
                FlowValidationIssue(
                    f"nodes.{node_id}",
                    "todas las ramas deben finalizar en texto terminal o accion",
                )
            )
    return issues


def _options(node: FlowNode) -> list[TargetOption]:
    if isinstance(node, ReplyButtonNode):
        return list(node.options)
    if isinstance(node, ListNode):
        return [option for section in node.sections for option in section.options]
    return []


def _text_fields(definition: FlowDefinition):
    for node_index, node in enumerate(definition.nodes):
        yield f"nodes.{node_index}.body", node.body
        if isinstance(node, (ReplyButtonNode, ListNode)):
            if node.header:
                yield f"nodes.{node_index}.header", node.header
            if node.footer:
                yield f"nodes.{node_index}.footer", node.footer
        if isinstance(node, ReplyButtonNode):
            for option_index, option in enumerate(node.options):
                yield f"nodes.{node_index}.options.{option_index}.title", option.title
        if isinstance(node, ListNode):
            yield f"nodes.{node_index}.button", node.button
            for section_index, section in enumerate(node.sections):
                if section.title:
                    yield (
                        f"nodes.{node_index}.sections.{section_index}.title",
                        section.title,
                    )
                for option_index, option in enumerate(section.options):
                    prefix = (
                        f"nodes.{node_index}.sections.{section_index}.options."
                        f"{option_index}"
                    )
                    yield f"{prefix}.title", option.title
                    if option.description:
                        yield f"{prefix}.description", option.description


def _template_issues(
    definition: FlowDefinition,
    policy: EventPolicy,
) -> list[FlowValidationIssue]:
    issues: list[FlowValidationIssue] = []
    formatter = Formatter()
    for path, text in _text_fields(definition):
        if re.search(r"https?://", text, flags=re.IGNORECASE):
            issues.append(
                FlowValidationIssue(
                    path,
                    "las URLs deben llegar mediante una variable permitida del backend",
                )
            )
        try:
            parsed = formatter.parse(text)
            for _literal, field_name, format_spec, conversion in parsed:
                if field_name is None:
                    continue
                if (
                    not field_name.isidentifier()
                    or format_spec
                    or conversion is not None
                ):
                    issues.append(
                        FlowValidationIssue(
                            path, "la variable debe ser un identificador simple"
                        )
                    )
                elif field_name not in policy.variables:
                    issues.append(
                        FlowValidationIssue(
                            path,
                            f"la variable {{{field_name}}} no esta permitida para el evento",
                        )
                    )
        except ValueError:
            issues.append(FlowValidationIssue(path, "la plantilla tiene llaves invalidas"))
    return issues


def _reachable_nodes(start_node: str, nodes_by_id: dict[str, FlowNode]) -> set[str]:
    pending = [start_node]
    reachable: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(
            option.next_node
            for option in _options(nodes_by_id[node_id])
            if option.next_node is not None
        )
    return reachable


def _all_paths_terminate(
    node_id: str,
    nodes_by_id: dict[str, FlowNode],
    visiting: set[str],
    cache: dict[str, bool],
) -> bool:
    if node_id in cache:
        return cache[node_id]
    if node_id in visiting:
        return False
    node = nodes_by_id[node_id]
    if isinstance(node, TextNode):
        cache[node_id] = True
        return True

    next_visiting = visiting | {node_id}
    result = all(
        option.action is not None
        or _all_paths_terminate(option.next_node, nodes_by_id, next_visiting, cache)
        for option in _options(node)
    )
    cache[node_id] = result
    return result
