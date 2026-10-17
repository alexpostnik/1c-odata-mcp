"""CSDL metadata parsing and payload validation for 1C OData."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from uuid import UUID
from xml.etree import ElementTree


_UNSAFE_XML = re.compile(r"<!\s*(doctype|entity)\b", re.IGNORECASE)
_COLLECTION_TYPE = re.compile(r"^Collection\s*\((?P<type>.*)\)\s*$", re.IGNORECASE)
_JSON_INTEGER = re.compile(r"^-?(?:0|[1-9][0-9]*)$")


def _local_name(name: str) -> str:
    if "}" in name:
        return name.rsplit("}", 1)[1]
    if ":" in name:
        return name.rsplit(":", 1)[1]
    return name


def _attr(element: ElementTree.Element, name: str, default: str | None = None) -> str | None:
    if name in element.attrib:
        return element.attrib[name]
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return value
    return default


def _bool_attr(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"false", "0", "no"}


def _children(element: ElementTree.Element, local_name: str | None = None) -> list[ElementTree.Element]:
    result = list(element)
    if local_name is None:
        return result
    return [child for child in result if _local_name(child.tag) == local_name]


def _unwrap_collection(type_name: str | None) -> tuple[bool, str]:
    value = (type_name or "").strip()
    match = _COLLECTION_TYPE.match(value)
    if not match:
        return False, value
    return True, match.group("type").strip()


def _qualified(namespace: str, name: str) -> str:
    return f"{namespace}.{name}" if namespace else name


def _is_simple_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _clean_1c_identifier(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError(f"invalid {label}")
    if "." in value:
        raise ValueError(f"{label} must not contain dots")
    return value


def _normalize_enum_name(value: str) -> str:
    value = value.strip()
    if value.startswith("StandardODATA."):
        value = value[len("StandardODATA."):]
    for prefix in ("EnumRef.", "EnumRef_", "Enum_"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return _clean_1c_identifier(value, "enum_name")


def _enum_odata_type(enum_name: str) -> str:
    return f"StandardODATA.EnumRef_{enum_name}"


def _enum_name_from_odata_type(type_name: str) -> str | None:
    if not isinstance(type_name, str):
        return None
    if type_name.startswith(("StandardODATA.EnumRef_", "StandardODATA.EnumRef.", "EnumRef.")):
        candidate = type_name
    elif type_name.startswith("StandardODATA."):
        # Some 1C configurations dispatch an EnumRef through the bare enum name.
        # Callers must still verify that the parsed name exists in CSDL.
        candidate = type_name[len("StandardODATA."):]
    else:
        return None
    try:
        return _normalize_enum_name(candidate)
    except ValueError:
        return None


def _enum_type_candidates(enum_name: str) -> list[str]:
    preferred = _enum_odata_type(enum_name)
    candidates = [
        preferred,
        f"StandardODATA.Enum_{enum_name}",
        f"StandardODATA.EnumRef.{enum_name}",
        f"EnumRef.{enum_name}",
    ]
    return list(dict.fromkeys(candidates))




@dataclass(frozen=True)
class PropertyModel:
    name: str
    type_name: str
    nullable: bool = True
    collection: bool = False
    kind: str = "property"
    declaring_type: str = ""
    max_length: str | None = None
    precision: str | None = None
    scale: str | None = None
    partner: str | None = None
    relationship: str | None = None
    from_role: str | None = None
    to_role: str | None = None
    multiplicity: str | None = None
    target_type: str | None = None
    target_entity_set: str | None = None

    def to_dict(self, model: "MetadataModel") -> dict[str, Any]:
        type_name = model.resolve_type_name(self.type_name, self.declaring_namespace)
        target_type = model.resolve_type_name(self.target_type or self.type_name, self.declaring_namespace)
        result: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "type": type_name,
            "nullable": self.nullable,
            "collection": self.collection,
        }
        for key, value in (
            ("max_length", self.max_length),
            ("precision", self.precision),
            ("scale", self.scale),
            ("partner", self.partner),
            ("relationship", self.relationship),
            ("from_role", self.from_role),
            ("to_role", self.to_role),
            ("multiplicity", self.multiplicity),
            ("target_type", target_type if target_type else None),
            ("target_entity_set", self.target_entity_set),
        ):
            if value not in (None, ""):
                result[key] = value
        return result

    @property
    def declaring_namespace(self) -> str:
        if "." not in self.declaring_type:
            return ""
        return self.declaring_type.rsplit(".", 1)[0]


@dataclass(frozen=True)
class StructuredTypeModel:
    name: str
    namespace: str
    kind: str
    base_type: str | None = None
    abstract: bool = False
    open_type: bool = False
    keys: tuple[str, ...] = ()
    properties: dict[str, PropertyModel] = field(default_factory=dict)
    navigation_properties: dict[str, PropertyModel] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return _qualified(self.namespace, self.name)


@dataclass(frozen=True)
class EntitySetModel:
    name: str
    entity_type: str
    container: str
    namespace: str
    navigation_bindings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EnumTypeModel:
    name: str
    namespace: str
    members: tuple[str, ...] = ()

    @property
    def qualified_name(self) -> str:
        return _qualified(self.namespace, self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualifiedName": self.qualified_name,
            "members": list(self.members),
        }


@dataclass(frozen=True)
class AssociationModel:
    name: str
    namespace: str
    ends: dict[str, dict[str, str | None]] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return _qualified(self.namespace, self.name)


@dataclass(frozen=True)
class OperationModel:
    name: str
    kind: str
    namespace: str = ""
    is_bound: bool = False
    entity_set: str | None = None
    operation: str | None = None
    return_type: str | None = None
    return_collection: bool = False
    binding_type: str | None = None
    binding_collection: bool = False
    side_effecting: bool | None = None
    http_method: str | None = None
    parameters: tuple[dict[str, Any], ...] = ()

    @property
    def qualified_name(self) -> str:
        return _qualified(self.namespace, self.name)

    def to_dict(self, model: "MetadataModel") -> dict[str, Any]:
        default_namespace = self.namespace
        result: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "bound": self.is_bound,
        }
        for key, value in (
            ("entity_set", self.entity_set),
            ("operation", model.resolve_type_name(self.operation, default_namespace)),
            ("return_type", model.resolve_type_name(self.return_type, default_namespace)),
            ("binding_type", model.resolve_type_name(self.binding_type, default_namespace)),
            ("http_method", self.http_method),
        ):
            if value not in (None, ""):
                result[key] = value
        if self.side_effecting is not None:
            result["side_effecting"] = self.side_effecting
        if self.return_type:
            result["return_collection"] = self.return_collection
        if self.binding_type:
            result["binding_collection"] = self.binding_collection
        if self.parameters:
            result["parameters"] = [
                {
                    **parameter,
                    "type": model.resolve_type_name(parameter.get("type"), default_namespace),
                }
                for parameter in self.parameters
            ]
        return result


class MetadataModel:
    """Namespace-aware CSDL model with conservative payload validation."""

    def __init__(
        self,
        *,
        aliases: dict[str, str] | None = None,
        types: dict[str, StructuredTypeModel] | None = None,
        entity_sets: dict[str, EntitySetModel] | None = None,
        enum_types: dict[str, EnumTypeModel] | None = None,
        associations: dict[str, AssociationModel] | None = None,
        operation_imports: list[OperationModel] | None = None,
        bound_operations: list[OperationModel] | None = None,
        enum_context_mappings: dict[tuple[str, str, str, str], dict[str, str]] | None = None,
    ) -> None:
        self.enum_context_mappings = {key: dict(value) for key, value in (enum_context_mappings or {}).items()}
        self.aliases = aliases or {}
        self.types = types or {}
        self.entity_sets = entity_sets or {}
        self.enum_types = enum_types or {}
        self.associations = associations or {}
        self.operation_imports = operation_imports or []
        self.bound_operations = bound_operations or []
        self._tabular_row_entity_sets: dict[str, dict[str, str]] = {}
        self._entity_set_by_type: dict[str, str] = {
            entity_set.entity_type: entity_set.name for entity_set in self.entity_sets.values()
        }
        self._types_by_local_name: dict[str, list[str]] = {}
        for qualified_name, type_model in self.types.items():
            self._types_by_local_name.setdefault(type_model.name, []).append(qualified_name)
        self._enum_types_by_local_name: dict[str, list[str]] = {}
        for qualified_name, enum_model in self.enum_types.items():
            self._enum_types_by_local_name.setdefault(enum_model.name, []).append(qualified_name)

    @classmethod
    def from_xml(cls, xml: str | bytes, *, enum_context_mappings: dict[tuple[str, str, str, str], dict[str, str]] | None = None) -> "MetadataModel":
        if isinstance(xml, bytes):
            sniff = xml[:4096].decode("utf-8", errors="ignore")
        else:
            sniff = xml[:4096]
        if _UNSAFE_XML.search(sniff):
            raise ValueError("CSDL metadata must not contain DOCTYPE or ENTITY declarations")

        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise ValueError(f"invalid CSDL XML: {exc}") from exc

        schemas = [element for element in root.iter() if _local_name(element.tag) == "Schema"]
        aliases: dict[str, str] = {}
        for schema in schemas:
            namespace = _attr(schema, "Namespace", "") or ""
            alias = _attr(schema, "Alias")
            if alias:
                aliases[alias] = namespace

        model = cls(aliases=aliases, enum_context_mappings=enum_context_mappings)
        types: dict[str, StructuredTypeModel] = {}
        enum_types: dict[str, EnumTypeModel] = {}
        associations: dict[str, AssociationModel] = {}
        entity_sets: dict[str, EntitySetModel] = {}
        operation_imports: list[OperationModel] = []
        bound_operations: list[OperationModel] = []

        for schema in schemas:
            namespace = _attr(schema, "Namespace", "") or ""
            for child in _children(schema):
                local = _local_name(child.tag)
                if local in {"EntityType", "ComplexType"}:
                    type_model = model._parse_structured_type(child, namespace, local)
                    types[type_model.qualified_name] = type_model
                elif local == "EnumType":
                    enum_model = model._parse_enum_type(child, namespace)
                    enum_types[enum_model.qualified_name] = enum_model

        model.types = types
        model.enum_types = enum_types
        model._types_by_local_name = {}
        for qualified_name, type_model in types.items():
            model._types_by_local_name.setdefault(type_model.name, []).append(qualified_name)
        model._enum_types_by_local_name = {}
        for qualified_name, enum_model in enum_types.items():
            model._enum_types_by_local_name.setdefault(enum_model.name, []).append(qualified_name)

        for schema in schemas:
            namespace = _attr(schema, "Namespace", "") or ""
            for child in _children(schema):
                local = _local_name(child.tag)
                if local == "Association":
                    association = model._parse_association(child, namespace)
                    associations[association.qualified_name] = association
                elif local in {"Action", "Function"}:
                    operation = model._parse_bound_operation(child, namespace, local.lower())
                    if operation is not None:
                        bound_operations.append(operation)

        model.associations = associations
        model.bound_operations = bound_operations

        for schema in schemas:
            namespace = _attr(schema, "Namespace", "") or ""
            for container in _children(schema, "EntityContainer"):
                container_name = _attr(container, "Name", "") or ""
                for child in _children(container):
                    local = _local_name(child.tag)
                    if local == "EntitySet":
                        entity_set = model._parse_entity_set(child, container_name, namespace)
                        entity_sets[entity_set.name] = entity_set
                    elif local in {"FunctionImport", "ActionImport"}:
                        operation_imports.append(
                            model._parse_operation_import(
                                child,
                                namespace,
                                "function_import" if local == "FunctionImport" else "action_import",
                            )
                        )

        model.entity_sets = entity_sets
        model._entity_set_by_type = {
            entity_set.entity_type: entity_set.name for entity_set in entity_sets.values()
        }
        model.operation_imports = operation_imports
        model._resolve_navigation_properties()
        model._tabular_row_entity_sets = model._build_tabular_row_index()
        return model

    def list_entity_sets(self, prefix: str | None = None) -> list[dict[str, Any]]:
        names = sorted(self.entity_sets)
        if prefix is not None:
            names = [name for name in names if name.startswith(prefix)]
        return [self._entity_set_summary(self.entity_sets[name]) for name in names]

    def describe_entity_set(self, name: str) -> dict[str, Any]:
        entity_set = self._require_entity_set(name)
        entity_type = self._require_type(entity_set.entity_type)
        properties, navigation_properties = self._all_fields(entity_type.qualified_name)
        keys = self._all_keys(entity_type.qualified_name)
        operations = self._operations_for_entity_set(entity_set)
        tabular_sections = self._tabular_sections(properties)
        tabular_row = self._tabular_row_entity_sets.get(entity_set.name)
        is_tabular_row = tabular_row is not None
        tabular_section = tabular_row.get("tabularSection") if tabular_row else None
        parent_document = tabular_row.get("parentDocument") if tabular_row else None
        property_dicts = self._decorate_composite_property_pairs(
            [field.to_dict(self) for field in properties.values()]
        )
        return {
            "name": entity_set.name,
            "container": entity_set.container,
            "namespace": entity_set.namespace,
            "entity_type": self.resolve_type_name(entity_set.entity_type, entity_set.namespace),
            "entity_type_name": entity_type.name,
            "isTabularRowEntity": is_tabular_row,
            "parentDocument": parent_document,
            "tabularSection": tabular_section,
            "keys": keys,
            "properties": property_dicts,
            "navigation_properties": [field.to_dict(self) for field in navigation_properties.values()],
            "tabularSections": tabular_sections,
            "function_imports": [
                operation.to_dict(self)
                for operation in operations
                if operation.kind == "function_import"
            ],
            "action_imports": [
                operation.to_dict(self)
                for operation in operations
                if operation.kind == "action_import"
            ],
            "actions": [operation.to_dict(self) for operation in operations],
        }

    def enum_property_compatibility(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Report enum dispatch pairs whose field-specific allowed types are unknown."""
        entity_set = self._require_entity_set(name)
        if not isinstance(payload, dict):
            return []
        return self._enum_property_compatibility_in_type(
            entity_set.entity_type,
            payload,
            path=name,
        )

    def _enum_property_compatibility_in_type(
        self,
        type_name: str,
        payload: dict[str, Any],
        *,
        path: str,
    ) -> list[dict[str, Any]]:
        resolved_type = self.resolve_type_name(type_name)
        if resolved_type not in self.types:
            return []
        type_model = self._require_type(resolved_type)
        properties, navigation_properties = self._all_fields(type_model.qualified_name)
        fields = {**properties, **navigation_properties}
        results: list[dict[str, Any]] = []

        for field_name, value in payload.items():
            if not isinstance(field_name, str):
                continue
            if field_name.endswith("_Type") and isinstance(value, str):
                base_name = field_name[: -len("_Type")]
                enum_name = _enum_name_from_odata_type(value)
                enum_model = self._find_enum_type(enum_name) if enum_name else None
                is_enum_ref = value.startswith((
                    "StandardODATA.EnumRef_",
                    "StandardODATA.EnumRef.",
                    "EnumRef.",
                ))
                if not is_enum_ref and enum_model is None:
                    enum_name = None
                business_field = properties.get(base_name)
                type_field = properties.get(field_name)
                if (
                    enum_name
                    and business_field is not None
                    and type_field is not None
                    and f"{base_name}_Base64Data" not in properties
                ):
                    business_type = self.resolve_type_name(
                        business_field.type_name,
                        business_field.declaring_namespace,
                    )
                    dispatch_type = self.resolve_type_name(
                        type_field.type_name,
                        type_field.declaring_namespace,
                    )
                    if business_type == "Edm.String" and dispatch_type == "Edm.String":
                        member_name = payload.get(base_name)
                        results.append({
                            "path": f"{path}.{base_name}",
                            "field": base_name,
                            "typeField": field_name,
                            "requestedType": value,
                            "enumMetadataVerified": enum_model is not None,
                            "memberMetadataVerified": bool(
                                enum_model
                                and enum_model.members
                                and isinstance(member_name, str)
                                and member_name in enum_model.members
                            ),
                            "propertyCompatibility": "unknown",
                            "reason": (
                                "The current metadata model exposes the composite dispatch field as "
                                "Edm.String but has no property-specific allowed runtime type set. "
                                "Enum/member metadata verification does not prove property compatibility."
                            ),
                            "requiredEvidence": (
                                "The allowed 1C ValueType set for this exact field in its "
                                "configuration context, such as the applicable chart-of-accounts analytics type."
                            ),
                        })

            field_model = fields.get(field_name)
            if field_model is None:
                continue
            nested_type = self.resolve_type_name(
                field_model.target_type or field_model.type_name,
                field_model.declaring_namespace,
            )
            if nested_type not in self.types:
                continue
            if field_model.collection and isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        results.extend(self._enum_property_compatibility_in_type(
                            nested_type,
                            item,
                            path=f"{path}.{field_name}[{index}]",
                        ))
            elif not field_model.collection and isinstance(value, dict):
                results.extend(self._enum_property_compatibility_in_type(
                    nested_type,
                    value,
                    path=f"{path}.{field_name}",
                ))

        return results

    def has_action(self, entity_set: str, action: str) -> bool:
        if action == "":
            return False
        try:
            operations = self.describe_entity_set(entity_set)["actions"]
        except ValueError:
            return False
        return any(operation["name"] == action for operation in operations)

    def validate_payload(self, name: str, payload: dict[str, Any]) -> None:
        entity_set = self._require_entity_set(name)
        if not isinstance(payload, dict):
            raise ValueError(f"payload for {name!r} must be an object")
        expanded = self._expand_payload_shorthands(entity_set.entity_type, payload, path=name)
        self._validate_object(entity_set.entity_type, expanded, path=name)

    def normalize_payload(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        entity_set = self._require_entity_set(name)
        if not isinstance(payload, dict):
            raise ValueError(f"payload for {name!r} must be an object")
        expanded = self._expand_payload_shorthands(entity_set.entity_type, payload, path=name)
        self._validate_object(entity_set.entity_type, expanded, path=name)
        return self._normalize_object(entity_set.entity_type, expanded)

    def resolve_enum_value(
        self,
        enum_name: str,
        member_name: str,
        *,
        field: str | None = None,
        account_context: str | None = None,
    ) -> dict[str, Any]:
        enum_name = _normalize_enum_name(enum_name)
        member_name = _clean_1c_identifier(member_name, "member_name")
        if field is not None:
            field = _clean_1c_identifier(field, "field")
        enum_model = self._find_enum_type(enum_name)
        if enum_model is not None and enum_model.members and member_name not in enum_model.members:
            raise ValueError(f"member_name {member_name!r} is not declared in enum {enum_name!r}")
        runtime_type = _enum_odata_type(enum_name)
        context_mapping: dict[str, str] | None = None
        if account_context is not None:
            account_context = _clean_1c_identifier(account_context, "account_context")
            if field is None:
                raise ValueError("account_context requires field")
            context_mapping = self.enum_context_mappings.get(
                (enum_name, member_name, field, account_context)
            )
            if context_mapping is None:
                raise ValueError(
                    "no owner-confirmed EnumRef dispatch mapping for this enum, member, field, and account_context"
                )
            runtime_type = context_mapping["odataType"]
        result: dict[str, Any] = {
            "enumName": enum_name,
            "memberName": member_name,
            "xmlType": f"EnumRef.{enum_name}",
            "odataType": runtime_type,
            "odataTypeCandidates": [
                runtime_type,
                *[candidate for candidate in _enum_type_candidates(enum_name) if candidate != runtime_type],
            ],
            "metadataVerified": enum_model is not None,
            "memberVerified": bool(enum_model and enum_model.members),
            "source": "1C OData composite dispatch _Type; enum XML type is EnumRef.<name>",
        }
        if context_mapping is not None:
            result["accountContext"] = account_context
            result["propertyCompatibility"] = "observed_for_context"
            result["propertyCompatibilityReason"] = context_mapping["reason"]
            result["propertyCompatibilityEvidence"] = context_mapping["evidence"]
        if enum_model is not None:
            result["enum"] = enum_model.to_dict()
        if field is not None:
            result["field"] = field
            result["typeField"] = f"{field}_Type"
            if context_mapping is None:
                result["propertyCompatibility"] = "unknown"
                result["propertyCompatibilityReason"] = (
                    "Enum/member verification does not prove that a particular composite property "
                    "allows this runtime type; the current metadata model has no property-specific allowed type set."
                )
            result["payloadFragment"] = {
                field: member_name,
                f"{field}_Type": result["odataType"],
            }
            result["payloadShorthand"] = {
                field: {
                    "onec83ValueType": "EnumRef",
                    "enum": enum_name,
                    "member": member_name,
                    **({"accountContext": account_context} if account_context is not None else {}),
                }
            }
        return result

    def resolve_type_name(self, type_name: str | None, default_namespace: str = "") -> str:
        if not type_name:
            return ""
        is_collection, inner = _unwrap_collection(type_name)
        if is_collection:
            resolved = self.resolve_type_name(inner, default_namespace)
            return f"Collection({resolved})"
        if inner := type_name.strip():
            if inner.startswith("Edm."):
                return inner
            if "." in inner:
                prefix, local = inner.rsplit(".", 1)
                namespace = self.aliases.get(prefix, prefix)
                return _qualified(namespace, local)
            candidate = _qualified(default_namespace, inner)
            if candidate in self.types:
                return candidate
            matches = self._types_by_local_name.get(inner, [])
            if len(matches) == 1:
                return matches[0]
            return inner
        return ""

    def _parse_enum_type(self, element: ElementTree.Element, namespace: str) -> EnumTypeModel:
        name = _attr(element, "Name")
        if not name:
            raise ValueError(f"EnumType without Name in namespace {namespace!r}")
        members = tuple(
            member_name
            for member_name in (_attr(member, "Name") for member in _children(element, "Member"))
            if member_name
        )
        return EnumTypeModel(name=name, namespace=namespace, members=members)

    def _parse_structured_type(
        self,
        element: ElementTree.Element,
        namespace: str,
        kind: str,
    ) -> StructuredTypeModel:
        name = _attr(element, "Name")
        if not name:
            raise ValueError(f"{kind} without Name in namespace {namespace!r}")
        qualified_name = _qualified(namespace, name)
        keys = tuple(
            key
            for key in (
                _attr(property_ref, "Name")
                for key_element in _children(element, "Key")
                for property_ref in _children(key_element, "PropertyRef")
            )
            if key
        )
        properties: dict[str, PropertyModel] = {}
        navigation_properties: dict[str, PropertyModel] = {}
        for child in _children(element):
            local = _local_name(child.tag)
            if local == "Property":
                property_model = self._parse_property(child, qualified_name)
                properties[property_model.name] = property_model
            elif local == "NavigationProperty":
                navigation_model = self._parse_navigation_property(child, qualified_name)
                navigation_properties[navigation_model.name] = navigation_model
        return StructuredTypeModel(
            name=name,
            namespace=namespace,
            kind="entity" if kind == "EntityType" else "complex",
            base_type=self.resolve_type_name(_attr(element, "BaseType"), namespace) or None,
            abstract=_bool_attr(_attr(element, "Abstract"), False),
            open_type=_bool_attr(_attr(element, "OpenType"), False),
            keys=keys,
            properties=properties,
            navigation_properties=navigation_properties,
        )

    def _parse_property(self, element: ElementTree.Element, declaring_type: str) -> PropertyModel:
        name = _attr(element, "Name")
        if not name:
            raise ValueError(f"Property without Name on {declaring_type}")
        collection, type_name = _unwrap_collection(_attr(element, "Type"))
        return PropertyModel(
            name=name,
            type_name=type_name,
            nullable=_bool_attr(_attr(element, "Nullable"), True),
            collection=collection,
            kind="property",
            declaring_type=declaring_type,
            max_length=_attr(element, "MaxLength"),
            precision=_attr(element, "Precision"),
            scale=_attr(element, "Scale"),
        )

    def _parse_navigation_property(
        self,
        element: ElementTree.Element,
        declaring_type: str,
    ) -> PropertyModel:
        name = _attr(element, "Name")
        if not name:
            raise ValueError(f"NavigationProperty without Name on {declaring_type}")
        collection, type_name = _unwrap_collection(_attr(element, "Type"))
        return PropertyModel(
            name=name,
            type_name=type_name,
            nullable=_bool_attr(_attr(element, "Nullable"), True),
            collection=collection,
            kind="navigation",
            declaring_type=declaring_type,
            partner=_attr(element, "Partner"),
            relationship=_attr(element, "Relationship"),
            from_role=_attr(element, "FromRole"),
            to_role=_attr(element, "ToRole"),
            target_type=type_name or None,
        )

    def _parse_association(self, element: ElementTree.Element, namespace: str) -> AssociationModel:
        name = _attr(element, "Name")
        if not name:
            raise ValueError(f"Association without Name in namespace {namespace!r}")
        ends: dict[str, dict[str, str | None]] = {}
        for end in _children(element, "End"):
            role = _attr(end, "Role")
            if not role:
                continue
            ends[role] = {
                "type": self.resolve_type_name(_attr(end, "Type"), namespace),
                "multiplicity": _attr(end, "Multiplicity"),
            }
        return AssociationModel(name=name, namespace=namespace, ends=ends)

    def _parse_entity_set(
        self,
        element: ElementTree.Element,
        container_name: str,
        namespace: str,
    ) -> EntitySetModel:
        name = _attr(element, "Name")
        entity_type = self.resolve_type_name(_attr(element, "EntityType"), namespace)
        if not name or not entity_type:
            raise ValueError(f"EntitySet without Name or EntityType in {container_name!r}")
        bindings: dict[str, str] = {}
        for binding in _children(element, "NavigationPropertyBinding"):
            path = _attr(binding, "Path")
            target = _attr(binding, "Target")
            if path and target:
                bindings[path] = target
        return EntitySetModel(
            name=name,
            entity_type=entity_type,
            container=container_name,
            namespace=namespace,
            navigation_bindings=bindings,
        )

    def _parse_operation_import(
        self,
        element: ElementTree.Element,
        namespace: str,
        kind: str,
    ) -> OperationModel:
        name = _attr(element, "Name")
        if not name:
            raise ValueError(f"{kind} without Name in namespace {namespace!r}")
        return_collection, return_type = _unwrap_collection(_attr(element, "ReturnType"))
        parameters = tuple(self._parse_parameters(element, namespace))
        is_bound = _bool_attr(_attr(element, "IsBindable") or _attr(element, "IsBound"), False)
        binding_type: str | None = None
        binding_collection = False
        if is_bound and parameters:
            binding_type = parameters[0].get("type")
            binding_collection = bool(parameters[0].get("collection"))
        side_effecting = _attr(element, "IsSideEffecting")
        return OperationModel(
            name=name,
            kind=kind,
            namespace=namespace,
            is_bound=is_bound,
            entity_set=_attr(element, "EntitySet"),
            operation=_attr(element, "Action") or _attr(element, "Function"),
            return_type=return_type or None,
            return_collection=return_collection,
            binding_type=binding_type or None,
            binding_collection=binding_collection,
            side_effecting=None if side_effecting is None else _bool_attr(side_effecting, False),
            http_method=_attr(element, "HttpMethod") or _attr(element, "Method"),
            parameters=parameters,
        )

    def _parse_bound_operation(
        self,
        element: ElementTree.Element,
        namespace: str,
        kind: str,
    ) -> OperationModel | None:
        if not _bool_attr(_attr(element, "IsBound"), False):
            return None
        name = _attr(element, "Name")
        if not name:
            raise ValueError(f"bound {kind} without Name in namespace {namespace!r}")
        parameters = tuple(self._parse_parameters(element, namespace))
        binding_type: str | None = None
        binding_collection = False
        if parameters:
            binding_type = parameters[0].get("type")
            binding_collection = bool(parameters[0].get("collection"))
        return_collection = False
        return_type: str | None = None
        for child in _children(element, "ReturnType"):
            return_collection, return_type = _unwrap_collection(_attr(child, "Type"))
            break
        return OperationModel(
            name=name,
            kind=kind,
            namespace=namespace,
            is_bound=True,
            return_type=return_type or None,
            return_collection=return_collection,
            binding_type=binding_type or None,
            binding_collection=binding_collection,
            side_effecting=(
                None
                if _attr(element, "IsSideEffecting") is None
                else _bool_attr(_attr(element, "IsSideEffecting"), False)
            ),
            parameters=parameters,
        )

    def _parse_parameters(
        self,
        element: ElementTree.Element,
        namespace: str,
    ) -> list[dict[str, Any]]:
        parameters: list[dict[str, Any]] = []
        for parameter in _children(element, "Parameter"):
            name = _attr(parameter, "Name")
            type_name = _attr(parameter, "Type")
            if not name or not type_name:
                continue
            collection, inner_type = _unwrap_collection(type_name)
            parameters.append(
                {
                    "name": name,
                    "type": self.resolve_type_name(inner_type, namespace),
                    "collection": collection,
                    "nullable": _bool_attr(_attr(parameter, "Nullable"), True),
                }
            )
        return parameters

    def _resolve_navigation_properties(self) -> None:
        updated_types: dict[str, StructuredTypeModel] = {}
        target_sets_by_type = {
            entity_set.entity_type: entity_set.name for entity_set in self.entity_sets.values()
        }
        for qualified_name, type_model in self.types.items():
            if not type_model.navigation_properties:
                updated_types[qualified_name] = type_model
                continue
            resolved_navigation: dict[str, PropertyModel] = {}
            for name, navigation in type_model.navigation_properties.items():
                target_type = self.resolve_type_name(navigation.target_type, type_model.namespace)
                multiplicity = navigation.multiplicity
                collection = navigation.collection
                target_entity_set = None
                if navigation.relationship and navigation.to_role:
                    association_name = self.resolve_type_name(navigation.relationship, type_model.namespace)
                    association = self.associations.get(association_name)
                    if association:
                        end = association.ends.get(navigation.to_role)
                        if end:
                            target_type = self.resolve_type_name(end.get("type"), type_model.namespace)
                            multiplicity = end.get("multiplicity")
                            collection = collection or multiplicity == "*"
                for entity_set in self.entity_sets.values():
                    binding_target = entity_set.navigation_bindings.get(name)
                    if entity_set.entity_type == qualified_name and binding_target:
                        target_entity_set = binding_target
                        break
                if target_entity_set is None and target_type:
                    target_entity_set = target_sets_by_type.get(target_type)
                resolved_navigation[name] = PropertyModel(
                    name=navigation.name,
                    type_name=target_type or navigation.type_name,
                    nullable=navigation.nullable,
                    collection=collection,
                    kind=navigation.kind,
                    declaring_type=navigation.declaring_type,
                    partner=navigation.partner,
                    relationship=navigation.relationship,
                    from_role=navigation.from_role,
                    to_role=navigation.to_role,
                    multiplicity=multiplicity,
                    target_type=target_type or navigation.target_type,
                    target_entity_set=target_entity_set,
                )
            updated_types[qualified_name] = StructuredTypeModel(
                name=type_model.name,
                namespace=type_model.namespace,
                kind=type_model.kind,
                base_type=type_model.base_type,
                abstract=type_model.abstract,
                open_type=type_model.open_type,
                keys=type_model.keys,
                properties=type_model.properties,
                navigation_properties=resolved_navigation,
            )
        self.types = updated_types

    def _entity_set_summary(self, entity_set: EntitySetModel) -> dict[str, Any]:
        entity_type = self._require_type(entity_set.entity_type)
        properties, navigation_properties = self._all_fields(entity_type.qualified_name)
        return {
            "name": entity_set.name,
            "container": entity_set.container,
            "namespace": entity_set.namespace,
            "entity_type": entity_type.qualified_name,
            "entity_type_name": entity_type.name,
            "isTabularRowEntity": entity_set.name in self._tabular_row_entity_sets,
            "keys": self._all_keys(entity_type.qualified_name),
            "property_count": len(properties),
            "navigation_property_count": len(navigation_properties),
        }

    def _all_fields(
        self,
        type_name: str,
        seen: tuple[str, ...] = (),
    ) -> tuple[dict[str, PropertyModel], dict[str, PropertyModel]]:
        type_model = self._require_type(type_name)
        if type_model.qualified_name in seen:
            raise ValueError(f"cyclic BaseType reference at {type_model.qualified_name}")
        properties: dict[str, PropertyModel] = {}
        navigation_properties: dict[str, PropertyModel] = {}
        if type_model.base_type:
            properties, navigation_properties = self._all_fields(
                type_model.base_type,
                (*seen, type_model.qualified_name),
            )
        properties = {**properties, **type_model.properties}
        navigation_properties = {**navigation_properties, **type_model.navigation_properties}
        return properties, navigation_properties

    def _all_keys(self, type_name: str, seen: tuple[str, ...] = ()) -> list[str]:
        type_model = self._require_type(type_name)
        if type_model.qualified_name in seen:
            raise ValueError(f"cyclic BaseType reference at {type_model.qualified_name}")
        keys: list[str] = []
        if type_model.base_type:
            keys.extend(self._all_keys(type_model.base_type, (*seen, type_model.qualified_name)))
        keys.extend(key for key in type_model.keys if key not in keys)
        return keys

    def _decorate_composite_property_pairs(
        self,
        properties: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        properties_by_name = {item.get("name"): item for item in properties}
        names = set(properties_by_name)
        decorated: list[dict[str, Any]] = []
        for item in properties:
            current = dict(item)
            name = current.get("name")
            type_field = properties_by_name.get(f"{name}_Type") if isinstance(name, str) else None
            is_string_pair = (
                current.get("type") == "Edm.String"
                and isinstance(type_field, dict)
                and type_field.get("type") == "Edm.String"
                and f"{name}_Base64Data" not in names
            )
            if isinstance(name, str) and is_string_pair:
                current["compositeTypeProperty"] = f"{name}_Type"
                current["valueShorthand"] = {
                    "enumRef": {
                        "shape": {
                            "onec83ValueType": "EnumRef",
                            "enum": "<1C enum metadata name>",
                            "member": "<1C enum member name>",
                        },
                        "resolverTool": "onec83_resolve_enum_value",
                    }
                }
            elif isinstance(name, str) and name.endswith("_Type"):
                base_name = name[: -len("_Type")]
                base_field = properties_by_name.get(base_name)
                if (
                    isinstance(base_field, dict)
                    and base_field.get("type") == "Edm.String"
                    and current.get("type") == "Edm.String"
                    and f"{base_name}_Base64Data" not in names
                ):
                    current["dispatchFor"] = base_name
                    current["allowedTypeStatus"] = "unknown"
                    current["allowedRuntimeTypes"] = None
                    current["allowedTypeSource"] = (
                        "The current metadata model declares the dispatch property as Edm.String but "
                        "does not expose a property-specific allowed 1C ValueType set"
                    )
            decorated.append(current)
        return decorated

    def _find_enum_type(self, enum_name: str) -> EnumTypeModel | None:
        direct_names = [enum_name, f"EnumRef.{enum_name}"]
        for name in direct_names:
            for qualified_name, enum_model in self.enum_types.items():
                if enum_model.qualified_name == name or enum_model.name == name:
                    return enum_model
        matches = self._enum_types_by_local_name.get(enum_name, [])
        if len(matches) == 1:
            return self.enum_types[matches[0]]
        return None

    def _enum_shorthand(self, value: Any) -> tuple[str, str, str | None] | None:
        if not isinstance(value, dict):
            return None
        kind = value.get("onec83ValueType") or value.get("kind")
        enum_name = value.get("enum") or value.get("enumName")
        member_name = value.get("member") or value.get("memberName") or value.get("value")
        if kind in {"EnumRef", "enumRef", "enum"} and enum_name and member_name:
            account_context = value.get("accountContext")
            if account_context is not None and not isinstance(account_context, str):
                raise ValueError("accountContext must be a string")
            return str(enum_name), str(member_name), account_context
        type_name = value.get("$type") or value.get("type")
        typed_value = value.get("$value") if "$value" in value else value.get("value")
        if isinstance(type_name, str) and type_name.startswith("EnumRef.") and typed_value:
            return type_name, str(typed_value), None
        return None

    def _expand_payload_shorthands(
        self,
        type_name: str,
        payload: dict[str, Any],
        *,
        path: str,
    ) -> dict[str, Any]:
        resolved_type = self.resolve_type_name(type_name)
        type_model = self._require_type(resolved_type)
        properties, navigation_properties = self._all_fields(type_model.qualified_name)
        fields = {**properties, **navigation_properties}
        expanded: dict[str, Any] = {}
        for field_name, value in payload.items():
            if not isinstance(field_name, str):
                raise ValueError(f"field names at {path} must be strings")
            if field_name in expanded:
                if expanded[field_name] != value:
                    raise ValueError(f"{path}.{field_name} conflicts with generated shorthand value")
                continue
            field_model = fields.get(field_name)
            shorthand = self._enum_shorthand(value)
            if shorthand is not None:
                if field_model is None:
                    raise ValueError(f"unknown field at {path}.{field_name}: not declared in metadata")
                type_field = f"{field_name}_Type"
                type_field_model = fields.get(type_field)
                if type_field_model is None:
                    raise ValueError(f"{path}.{field_name} has no matching {type_field} dispatch property")
                if self.resolve_type_name(field_model.type_name, field_model.declaring_namespace) != "Edm.String":
                    raise ValueError(f"{path}.{field_name} is not a string-valued composite field")
                if self.resolve_type_name(type_field_model.type_name, type_field_model.declaring_namespace) != "Edm.String":
                    raise ValueError(f"{path}.{type_field} is not an OData dispatch string")
                resolved_enum = self.resolve_enum_value(
                    shorthand[0],
                    shorthand[1],
                    field=field_name,
                    account_context=shorthand[2],
                )
                if type_field in payload and payload[type_field] != resolved_enum["odataType"]:
                    raise ValueError(f"{path}.{type_field} conflicts with enum shorthand type")
                expanded[field_name] = resolved_enum["memberName"]
                expanded[type_field] = resolved_enum["odataType"]
                continue
            if field_model is not None:
                expanded[field_name] = self._expand_value_shorthands(
                    field_model, value, path=f"{path}.{field_name}"
                )
            else:
                expanded[field_name] = value
        return expanded

    def _expand_value_shorthands(
        self,
        field_model: PropertyModel,
        value: Any,
        *,
        path: str,
    ) -> Any:
        if value is None:
            return None
        type_name = self.resolve_type_name(
            field_model.target_type or field_model.type_name,
            field_model.declaring_namespace,
        )
        if field_model.collection:
            if not isinstance(value, list):
                return value
            return [
                self._expand_payload_shorthands(type_name, item, path=f"{path}[{index}]")
                if isinstance(item, dict) and type_name in self.types else item
                for index, item in enumerate(value)
            ]
        if type_name in self.types and isinstance(value, dict):
            return self._expand_payload_shorthands(type_name, value, path=path)
        return value

    def _operations_for_entity_set(self, entity_set: EntitySetModel) -> list[OperationModel]:
        operations: list[OperationModel] = []
        for operation in self.operation_imports:
            if self._operation_import_applies_to_entity_set(operation, entity_set):
                operations.append(operation)
        for operation in self.bound_operations:
            if self._bound_operation_applies_to_entity_set(operation, entity_set):
                operations.append(operation)
        return operations

    def _tabular_sections(self, properties: dict[str, PropertyModel]) -> list[str]:
        sections: list[str] = []
        for name, property_model in properties.items():
            if not property_model.collection:
                continue
            type_name = self.resolve_type_name(
                property_model.target_type or property_model.type_name,
                property_model.declaring_namespace,
            )
            if type_name in self.types:
                sections.append(name)
        return sections

    def _build_tabular_row_index(self) -> dict[str, dict[str, str]]:
        candidates_by_row_set: dict[str, list[dict[str, str]]] = {}
        for parent_set in self.entity_sets.values():
            properties, _ = self._all_fields(parent_set.entity_type)
            for section_name, property_model in properties.items():
                if not property_model.collection:
                    continue
                row_type = self.resolve_type_name(
                    property_model.target_type or property_model.type_name,
                    property_model.declaring_namespace,
                )
                row_entity_set = self._row_entity_set_for_collection_type(row_type)
                if row_entity_set is None:
                    continue
                if row_entity_set != f"{parent_set.name}_{section_name}":
                    continue
                candidates_by_row_set.setdefault(row_entity_set, []).append(
                    {
                        "parentDocument": parent_set.name,
                        "tabularSection": section_name,
                    }
                )
        result: dict[str, dict[str, str]] = {}
        for row_entity_set, candidates in candidates_by_row_set.items():
            if len(candidates) == 1:
                result[row_entity_set] = candidates[0]
        return result

    def _row_entity_set_for_collection_type(self, row_type: str) -> str | None:
        direct_entity_set = self._entity_set_by_type.get(row_type)
        if direct_entity_set is not None:
            return direct_entity_set
        namespace = row_type.rsplit(".", 1)[0] if "." in row_type else ""
        local_type = row_type.rsplit(".", 1)[-1]
        if not local_type.endswith("_RowType"):
            return None
        row_entity_type = _qualified(namespace, local_type[: -len("_RowType")])
        return self._entity_set_by_type.get(row_entity_type)

    def _operation_import_applies_to_entity_set(
        self,
        operation: OperationModel,
        entity_set: EntitySetModel,
    ) -> bool:
        if operation.is_bound and self._bound_operation_applies_to_entity_set(operation, entity_set):
            return True
        if operation.entity_set == entity_set.name:
            return True
        if operation.operation:
            target = self.resolve_type_name(operation.operation, operation.namespace)
            bound = next(
                (
                    candidate
                    for candidate in self.bound_operations
                    if candidate.qualified_name == target
                ),
                None,
            )
            if bound is not None and self._bound_operation_applies_to_entity_set(bound, entity_set):
                return True
        if operation.return_type:
            return_type = self.resolve_type_name(operation.return_type, operation.namespace)
            return return_type == entity_set.entity_type
        return False

    def _bound_operation_applies_to_entity_set(
        self,
        operation: OperationModel,
        entity_set: EntitySetModel,
    ) -> bool:
        if not operation.binding_type:
            return False
        binding_type = self.resolve_type_name(operation.binding_type, operation.namespace)
        return binding_type == entity_set.entity_type

    def _validate_object(self, type_name: str, payload: dict[str, Any], path: str) -> None:
        resolved_type = self.resolve_type_name(type_name)
        type_model = self._require_type(resolved_type)
        properties, navigation_properties = self._all_fields(type_model.qualified_name)
        fields = {**properties, **navigation_properties}
        for field_name, value in payload.items():
            if field_name not in fields:
                raise ValueError(f"unknown field at {path}.{field_name}: not declared in metadata")
            self._validate_field(fields[field_name], value, f"{path}.{field_name}")

    def _validate_field(self, field_model: PropertyModel, value: Any, path: str) -> None:
        if value is None:
            if not field_model.nullable:
                raise ValueError(f"{path} must not be null")
            return
        if field_model.collection:
            if not isinstance(value, list):
                raise ValueError(f"{path} must be a list")
            for index, item in enumerate(value):
                self._validate_value(field_model, item, f"{path}[{index}]")
            return
        self._validate_value(field_model, value, path)

    def _validate_value(self, field_model: PropertyModel, value: Any, path: str) -> None:
        type_name = self.resolve_type_name(
            field_model.target_type or field_model.type_name,
            field_model.declaring_namespace,
        )
        if self._is_edm_type(type_name):
            self._validate_scalar(type_name, value, path)
            if type_name == "Edm.String" and field_model.max_length is not None:
                facet = field_model.max_length
                if facet.lower() != "max":
                    if not re.fullmatch(r"[0-9]+", facet):
                        raise ValueError(f"{path} has an unsupported MaxLength facet")
                    if len(value) > int(facet):
                        raise ValueError(f"{path} exceeds metadata MaxLength {facet}")
            return
        if type_name in self.types:
            if not isinstance(value, dict):
                raise ValueError(f"{path} must be an object")
            self._validate_object(type_name, value, path)
            return
        if field_model.kind == "navigation" and not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")

    def _validate_scalar(self, type_name: str, value: Any, path: str) -> None:
        local_type = type_name.rsplit(".", 1)[-1]
        valid = False
        if local_type == "String":
            valid = isinstance(value, str)
        elif local_type == "Boolean":
            valid = isinstance(value, bool)
        elif local_type in {"Byte", "SByte", "Int16", "Int32", "Int64"}:
            valid = self._is_int_for_type(value, local_type)
        elif local_type in {"Single", "Double"}:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif local_type == "Decimal":
            valid = self._is_decimal(value)
        elif local_type == "Guid":
            valid = self._is_guid(value)
        elif local_type in {"DateTime", "DateTimeOffset"}:
            valid = isinstance(value, (str, datetime))
        elif local_type == "Date":
            valid = isinstance(value, (str, date))
        elif local_type in {"Time", "TimeOfDay"}:
            valid = isinstance(value, (str, time))
        elif local_type == "Duration":
            valid = isinstance(value, str)
        elif local_type in {"Binary", "Stream"}:
            valid = isinstance(value, (str, bytes, bytearray, memoryview))
        else:
            valid = _is_simple_json_scalar(value)
        if not valid:
            raise ValueError(f"{path} must be {type_name}")

    def _is_edm_type(self, type_name: str) -> bool:
        return type_name.startswith("Edm.")

    def _is_int_for_type(self, value: Any, local_type: str) -> bool:
        return self._parse_int_for_type(value, local_type) is not None

    def _parse_int_for_type(self, value: Any, local_type: str) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            parsed = value
        elif local_type == "Int64" and isinstance(value, str) and _JSON_INTEGER.fullmatch(value):
            parsed = int(value)
        else:
            return None
        ranges = {
            "Byte": (0, 255),
            "SByte": (-128, 127),
            "Int16": (-32768, 32767),
            "Int32": (-2147483648, 2147483647),
            "Int64": (-9223372036854775808, 9223372036854775807),
        }
        low, high = ranges[local_type]
        if not low <= parsed <= high:
            return None
        return parsed

    def _normalize_object(self, type_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        resolved_type = self.resolve_type_name(type_name)
        type_model = self._require_type(resolved_type)
        properties, navigation_properties = self._all_fields(type_model.qualified_name)
        fields = {**properties, **navigation_properties}
        return {
            field_name: self._normalize_field(fields[field_name], value)
            for field_name, value in payload.items()
        }

    def _normalize_field(self, field_model: PropertyModel, value: Any) -> Any:
        if value is None:
            return None
        if field_model.collection:
            return [self._normalize_value(field_model, item) for item in value]
        return self._normalize_value(field_model, value)

    def _normalize_value(self, field_model: PropertyModel, value: Any) -> Any:
        type_name = self.resolve_type_name(
            field_model.target_type or field_model.type_name,
            field_model.declaring_namespace,
        )
        if self._is_edm_type(type_name):
            return self._normalize_scalar(type_name, value)
        if type_name in self.types:
            return self._normalize_object(type_name, value)
        return value

    def _normalize_scalar(self, type_name: str, value: Any) -> Any:
        local_type = type_name.rsplit(".", 1)[-1]
        if local_type != "Int64":
            return value
        parsed = self._parse_int_for_type(value, local_type)
        if parsed is None:
            return value
        return str(parsed)

    def _is_decimal(self, value: Any) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float, Decimal)):
            return True
        if isinstance(value, str):
            try:
                Decimal(value)
            except InvalidOperation:
                return False
            return True
        return False

    def _is_guid(self, value: Any) -> bool:
        if isinstance(value, UUID):
            return True
        if not isinstance(value, str):
            return False
        try:
            UUID(value)
        except ValueError:
            return False
        return True

    def _require_entity_set(self, name: str) -> EntitySetModel:
        try:
            return self.entity_sets[name]
        except KeyError as exc:
            raise ValueError(f"unknown entity set {name!r}") from exc

    def _require_type(self, type_name: str) -> StructuredTypeModel:
        resolved = self.resolve_type_name(type_name)
        try:
            return self.types[resolved]
        except KeyError as exc:
            raise ValueError(f"unknown structured type {type_name!r}") from exc


__all__ = ["MetadataModel"]
