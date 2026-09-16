import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.api.whatsapp import (
    OutboundWhatsAppMessage,
    WhatsAppList,
    WhatsAppListRow,
    WhatsAppListSection,
    WhatsAppReplyButton,
    WhatsAppReplyButtons,
    WhatsAppText,
    build_whatsapp_payload,
)
from app.services.conversation import (
    ConversationService,
    ConversationStateUnavailable,
    PendingConversationFlow,
)
from app.services.conversation_flow import (
    ConversationFlowNotFound,
    ConversationFlowService,
    FlowSnapshot,
    FlowVersionSnapshot,
)
from app.services.conversation_flow_contract import (
    ConversationFlowDefinitionInvalid,
    validate_flow_definition,
)


INTERACTION_UNAVAILABLE_REPLY = (
    "Esa opción ya no está disponible. Escribime de nuevo qué querés hacer."
)


@dataclass(frozen=True)
class ConfiguredFlowReply:
    reply_text: str = ""
    reply_message: OutboundWhatsAppMessage | None = None


class ConversationFlowRuntime:
    @classmethod
    async def render_event(
        cls,
        *,
        sender_phone: str,
        event_key: str,
        variables: dict[str, Any],
    ) -> OutboundWhatsAppMessage | None:
        try:
            flow = ConversationFlowService.find_published_by_event(event_key)
            if flow is None or flow.published is None:
                return None
            normalized_variables = {
                str(key): str(value) for key, value in variables.items()
            }
            return await cls._render_node(
                sender_phone=sender_phone,
                flow=flow,
                version=flow.published,
                node_id=flow.published.definition["start_node"],
                variables=normalized_variables,
            )
        except (
            ConversationFlowDefinitionInvalid,
            ConversationFlowNotFound,
            ConversationStateUnavailable,
            SQLAlchemyError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            print(f"[CONVERSATION_FLOW] render fallback: {type(exc).__name__}")
            return None

    @classmethod
    async def handle_reply(
        cls,
        *,
        sender_phone: str,
        option_id: str,
        reply_type: str,
        action_handler,
    ):
        try:
            pending = await ConversationService.get_pending_conversation_flow(
                sender_phone
            )
        except ConversationStateUnavailable:
            return ConfiguredFlowReply(reply_text=INTERACTION_UNAVAILABLE_REPLY)
        if pending is None:
            return ConfiguredFlowReply(reply_text=INTERACTION_UNAVAILABLE_REPLY)

        try:
            flow, version = ConversationFlowService.get_version(
                UUID(pending.flow_id),
                UUID(pending.version_id),
            )
            if flow.event_key != pending.event_key:
                return ConfiguredFlowReply(reply_text=INTERACTION_UNAVAILABLE_REPLY)
            definition = validate_flow_definition(
                flow.event_key,
                version.definition,
            )
            node = cls._node(definition, pending.node_id)
            if not cls._reply_type_matches(reply_type, node["type"]):
                return ConfiguredFlowReply(reply_text=INTERACTION_UNAVAILABLE_REPLY)
            option = cls._selected_option(version, node, option_id)
            if option is None:
                return ConfiguredFlowReply(reply_text=INTERACTION_UNAVAILABLE_REPLY)

            if option.get("action"):
                await ConversationService.clear_pending_conversation_flow(sender_phone)
                return await action_handler(option["action"])

            next_node = option.get("next_node")
            if not next_node:
                return ConfiguredFlowReply(reply_text=INTERACTION_UNAVAILABLE_REPLY)
            message = await cls._render_node(
                sender_phone=sender_phone,
                flow=flow,
                version=version,
                node_id=next_node,
                variables=pending.variables,
            )
            return ConfiguredFlowReply(reply_message=message)
        except (
            ConversationFlowDefinitionInvalid,
            ConversationFlowNotFound,
            ConversationStateUnavailable,
            SQLAlchemyError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            print(f"[CONVERSATION_FLOW] interactive rejected: {type(exc).__name__}")
            return ConfiguredFlowReply(reply_text=INTERACTION_UNAVAILABLE_REPLY)

    @staticmethod
    async def abandon(sender_phone: str) -> None:
        try:
            await ConversationService.clear_pending_conversation_flow(sender_phone)
        except ConversationStateUnavailable as exc:
            print(f"[CONVERSATION_FLOW] abandon unavailable: {type(exc).__name__}")

    @classmethod
    async def _render_node(
        cls,
        *,
        sender_phone: str,
        flow: FlowSnapshot,
        version: FlowVersionSnapshot,
        node_id: str,
        variables: dict[str, str],
    ) -> OutboundWhatsAppMessage:
        definition = validate_flow_definition(flow.event_key, version.definition)
        node = cls._node(definition, node_id)
        message = cls._message(version, node, variables)
        build_whatsapp_payload("0", message)

        if node["type"] == "text":
            await cls._clear_if_present(sender_phone)
        else:
            await ConversationService.set_pending_conversation_flow(
                sender_phone,
                PendingConversationFlow(
                    flow_id=str(flow.id),
                    version_id=str(version.id),
                    event_key=flow.event_key,
                    node_id=node_id,
                    variables=variables,
                ),
            )
        return message

    @staticmethod
    async def _clear_if_present(sender_phone: str) -> None:
        pending = await ConversationService.get_pending_conversation_flow(sender_phone)
        if pending is not None:
            await ConversationService.clear_pending_conversation_flow(sender_phone)

    @classmethod
    def _message(
        cls,
        version: FlowVersionSnapshot,
        node: dict,
        variables: dict[str, str],
    ) -> OutboundWhatsAppMessage:
        node_type = node["type"]
        body = cls._render_text(node["body"], variables)
        if node_type == "text":
            return WhatsAppText(body)
        if node_type == "reply_button":
            return WhatsAppReplyButtons(
                body=body,
                header=cls._render_optional(node.get("header"), variables),
                footer=cls._render_optional(node.get("footer"), variables),
                buttons=tuple(
                    WhatsAppReplyButton(
                        id=cls._option_token(version, node["id"], option["id"]),
                        title=cls._render_text(option["title"], variables),
                    )
                    for option in node["options"]
                ),
            )
        return WhatsAppList(
            body=body,
            button=cls._render_text(node["button"], variables),
            header=cls._render_optional(node.get("header"), variables),
            footer=cls._render_optional(node.get("footer"), variables),
            sections=tuple(
                WhatsAppListSection(
                    title=cls._render_optional(section.get("title"), variables),
                    rows=tuple(
                        WhatsAppListRow(
                            id=cls._option_token(
                                version,
                                node["id"],
                                option["id"],
                            ),
                            title=cls._render_text(option["title"], variables),
                            description=cls._render_optional(
                                option.get("description"),
                                variables,
                            ),
                        )
                        for option in section["options"]
                    ),
                )
                for section in node["sections"]
            ),
        )

    @classmethod
    def _selected_option(
        cls,
        version: FlowVersionSnapshot,
        node: dict,
        option_id: str,
    ) -> dict | None:
        for option in cls._options(node):
            expected = cls._option_token(version, node["id"], option["id"])
            if option_id == expected:
                return option
        return None

    @staticmethod
    def _options(node: dict) -> list[dict]:
        if node["type"] == "reply_button":
            return node["options"]
        if node["type"] == "list":
            return [
                option
                for section in node["sections"]
                for option in section["options"]
            ]
        return []

    @staticmethod
    def _node(definition: dict, node_id: str) -> dict:
        for node in definition["nodes"]:
            if node["id"] == node_id:
                return node
        raise KeyError(node_id)

    @staticmethod
    def _reply_type_matches(reply_type: str, node_type: str) -> bool:
        return (reply_type, node_type) in {
            ("button_reply", "reply_button"),
            ("list_reply", "list"),
        }

    @staticmethod
    def _option_token(
        version: FlowVersionSnapshot,
        node_id: str,
        option_id: str,
    ) -> str:
        raw = f"{version.id}:{node_id}:{option_id}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"cf.{version.id.hex}.{digest}"

    @staticmethod
    def _render_text(template: str, variables: dict[str, str]) -> str:
        return template.format_map(variables)

    @classmethod
    def _render_optional(
        cls,
        template: str | None,
        variables: dict[str, str],
    ) -> str | None:
        return cls._render_text(template, variables) if template is not None else None
