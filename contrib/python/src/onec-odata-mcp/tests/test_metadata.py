from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from onec_odata_mcp.metadata import MetadataModel


CSDL = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx
    Version="1.0"
    xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices>
    <Schema
        Namespace="Demo"
        Alias="Self"
        xmlns="http://schemas.microsoft.com/ado/2009/11/edm"
        xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
      <EntityType Name="Catalog_Base" Abstract="true">
        <Property Name="DeletionMark" Type="Edm.Boolean" Nullable="false" />
      </EntityType>

      <EnumType Name="ВидыПлатежейВГосБюджет">
        <Member Name="Налог" />
        <Member Name="Пени" />
      </EnumType>

      <EntityType Name="Catalog_Customer" BaseType="Demo.Catalog_Base">
        <Key>
          <PropertyRef Name="Ref_Key" />
        </Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false" />
        <Property Name="Description" Type="Edm.String" />
        <Property Name="Contacts" Type="Collection(Demo.Catalog_Customer_Contacts_RowType)" Nullable="false" />
      </EntityType>

      <ComplexType Name="Catalog_Customer_Contacts_RowType">
        <Property Name="LineNumber" Type="Edm.Int32" Nullable="false" />
        <Property Name="Contact" Type="Edm.String" />
      </ComplexType>

      <EntityType Name="Catalog_Customer_Contacts">
        <Key>
          <PropertyRef Name="Ref_Key" />
          <PropertyRef Name="LineNumber" />
        </Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false" />
        <Property Name="LineNumber" Type="Edm.Int32" Nullable="false" />
        <Property Name="Contact" Type="Edm.String" />
      </EntityType>

      <EntityType Name="Document_Order">
        <Key>
          <PropertyRef Name="Ref_Key" />
        </Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false" />
        <Property Name="Number" Type="Edm.String" Nullable="false" />
        <Property Name="Date" Type="Edm.DateTime" Nullable="false" />
        <Property Name="Posted" Type="Edm.Boolean" Nullable="false" />
        <Property Name="Total" Type="Edm.Decimal" Precision="15" Scale="2" />
        <Property Name="Lines" Type="Collection(Demo.Document_Order_Lines_RowType)" Nullable="false" />
        <NavigationProperty
            Name="Customer"
            Relationship="Demo.Document_Order_Customer"
            FromRole="Order"
            ToRole="Customer" />
      </EntityType>

      <ComplexType Name="Document_Order_Lines_RowType">
        <Property Name="LineNumber" Type="Edm.Int64" Nullable="false" />
        <Property Name="Item_Key" Type="Edm.Guid" />
        <Property Name="Multiplicity" Type="Edm.Int64" />
        <Property Name="Price" Type="Edm.Decimal" />
        <Property Name="СубконтоДт1" Type="Edm.String" />
        <Property Name="СубконтоДт1_Type" Type="Edm.String" />
      </ComplexType>

      <EntityType Name="Document_Order_Lines">
        <Key>
          <PropertyRef Name="Ref_Key" />
          <PropertyRef Name="LineNumber" />
        </Key>
        <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false" />
        <Property Name="LineNumber" Type="Edm.Int64" Nullable="false" />
        <Property Name="Item_Key" Type="Edm.Guid" />
        <Property Name="Multiplicity" Type="Edm.Int64" />
        <Property Name="Price" Type="Edm.Decimal" />
        <Property Name="СубконтоДт1" Type="Edm.String" />
        <Property Name="СубконтоДт1_Type" Type="Edm.String" />
      </EntityType>

      <Association Name="Document_Order_Customer">
        <End Type="Demo.Document_Order" Role="Order" Multiplicity="*" />
        <End Type="Self.Catalog_Customer" Role="Customer" Multiplicity="0..1" />
      </Association>

      <Action Name="Post" IsBound="true">
        <Parameter Name="bindingParameter" Type="Demo.Document_Order" Nullable="false" />
      </Action>

      <Function Name="AvailableActions" IsBound="true">
        <Parameter Name="bindingParameter" Type="Collection(Demo.Document_Order)" Nullable="false" />
        <ReturnType Type="Collection(Edm.String)" />
      </Function>

      <EntityContainer Name="DefaultContainer" m:IsDefaultEntityContainer="true">
        <EntitySet Name="Document_Order" EntityType="Demo.Document_Order">
          <NavigationPropertyBinding Path="Customer" Target="Catalog_Customer" />
        </EntitySet>
        <EntitySet Name="Document_Order_Lines" EntityType="Demo.Document_Order_Lines" />
        <EntitySet Name="Catalog_Customer" EntityType="Self.Catalog_Customer" />
        <EntitySet Name="Catalog_Customer_Contacts" EntityType="Demo.Catalog_Customer_Contacts" />
        <FunctionImport
            Name="Balance"
            ReturnType="Edm.Decimal"
            EntitySet="Document_Order"
            m:HttpMethod="GET">
          <Parameter Name="Moment" Type="Edm.DateTime" Nullable="false" />
        </FunctionImport>
        <FunctionImport Name="Repost" IsBindable="true" IsSideEffecting="true">
          <Parameter Name="bindingParameter" Type="Demo.Document_Order" Nullable="false" />
        </FunctionImport>
        <ActionImport Name="PostImported" Action="Demo.Post" EntitySet="Document_Order" />
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


class MetadataModelTests(unittest.TestCase):
    def test_lists_and_describes_entity_sets_with_actions(self) -> None:
        model = MetadataModel.from_xml(CSDL.encode("utf-8"))

        self.assertEqual(
            [item["name"] for item in model.list_entity_sets()],
            [
                "Catalog_Customer",
                "Catalog_Customer_Contacts",
                "Document_Order",
                "Document_Order_Lines",
            ],
        )
        self.assertEqual(
            [item["name"] for item in model.list_entity_sets(prefix="Document_")],
            ["Document_Order", "Document_Order_Lines"],
        )

        description = model.describe_entity_set("Document_Order")
        self.assertEqual(description["entity_type"], "Demo.Document_Order")
        self.assertEqual(description["keys"], ["Ref_Key"])

        properties = {item["name"]: item for item in description["properties"]}
        self.assertEqual(properties["Lines"]["type"], "Demo.Document_Order_Lines_RowType")
        self.assertTrue(properties["Lines"]["collection"])
        self.assertEqual(properties["Total"]["precision"], "15")
        self.assertEqual(description["tabularSections"], ["Lines"])
        self.assertFalse(description["isTabularRowEntity"])

        navigation = {item["name"]: item for item in description["navigation_properties"]}
        self.assertEqual(navigation["Customer"]["target_type"], "Demo.Catalog_Customer")
        self.assertEqual(navigation["Customer"]["target_entity_set"], "Catalog_Customer")

        actions = {(item["name"], item["kind"]) for item in description["actions"]}
        self.assertIn(("Post", "action"), actions)
        self.assertIn(("AvailableActions", "function"), actions)
        self.assertIn(("Balance", "function_import"), actions)
        self.assertIn(("Repost", "function_import"), actions)
        self.assertIn(("PostImported", "action_import"), actions)
        action_by_name = {item["name"]: item for item in description["actions"]}
        self.assertTrue(action_by_name["AvailableActions"]["binding_collection"])
        self.assertTrue(action_by_name["Repost"]["side_effecting"])
        self.assertTrue(model.has_action("Document_Order", "Post"))
        self.assertTrue(model.has_action("Document_Order", "Balance"))
        self.assertTrue(model.has_action("Document_Order", "Repost"))
        self.assertFalse(model.has_action("Catalog_Customer", "Repost"))
        self.assertFalse(model.has_action("Document_Order", "Unpost"))
        self.assertFalse(model.has_action("Missing", "Post"))

    def test_describes_reliable_tabular_row_entity_parent(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        summary = {
            item["name"]: item
            for item in model.list_entity_sets(prefix="Document_Order")
        }
        self.assertFalse(summary["Document_Order"]["isTabularRowEntity"])
        self.assertTrue(summary["Document_Order_Lines"]["isTabularRowEntity"])

        description = model.describe_entity_set("Document_Order_Lines")

        self.assertTrue(description["isTabularRowEntity"])
        self.assertEqual(description["parentDocument"], "Document_Order")
        self.assertEqual(description["tabularSection"], "Lines")
        self.assertEqual(description["tabularSections"], [])

    def test_describes_catalog_tabular_row_entity_parent(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        summary = {
            item["name"]: item
            for item in model.list_entity_sets(prefix="Catalog_Customer")
        }
        self.assertFalse(summary["Catalog_Customer"]["isTabularRowEntity"])
        self.assertTrue(summary["Catalog_Customer_Contacts"]["isTabularRowEntity"])

        parent = model.describe_entity_set("Catalog_Customer")
        self.assertEqual(parent["tabularSections"], ["Contacts"])
        self.assertFalse(parent["isTabularRowEntity"])

        description = model.describe_entity_set("Catalog_Customer_Contacts")

        self.assertTrue(description["isTabularRowEntity"])
        self.assertEqual(description["parentDocument"], "Catalog_Customer")
        self.assertEqual(description["tabularSection"], "Contacts")
        self.assertEqual(description["tabularSections"], [])

    def test_inherited_fields_are_available_for_describe_and_validation(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        description = model.describe_entity_set("Catalog_Customer")
        properties = {item["name"]: item for item in description["properties"]}

        self.assertIn("DeletionMark", properties)
        model.validate_payload(
            "Catalog_Customer",
            {
                "Ref_Key": "11111111-2222-4333-8444-000000000004",
                "Description": "Synthetic customer",
                "DeletionMark": False,
            },
        )

    def test_validates_partial_payloads_and_nested_collection_rows(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        model.validate_payload(
            "Document_Order",
            {
                "Ref_Key": "11111111-2222-4333-8444-555555555555",
                "Number": "000000001",
                "Date": "2026-09-28T12:00:00",
                "Total": "10.50",
                "Lines": [
                    {
                        "LineNumber": 1,
                        "Item_Key": "11111111-2222-4333-8444-000000000005",
                        "Multiplicity": "0",
                        "Price": 10.5,
                    }
                ],
                "Customer": {
                    "Ref_Key": "11111111-2222-4333-8444-000000000004",
                    "Description": "Synthetic customer",
                },
            },
        )

    def test_accepts_and_normalizes_odata_int64_strings(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        payload = {
            "Lines": [
                {
                    "LineNumber": "1",
                    "Item_Key": "11111111-2222-4333-8444-000000000005",
                    "Multiplicity": 0,
                    "Price": "10.50",
                }
            ]
        }

        model.validate_payload("Document_Order", payload)
        normalized = model.normalize_payload("Document_Order", payload)

        self.assertEqual(normalized["Lines"][0]["LineNumber"], "1")
        self.assertEqual(normalized["Lines"][0]["Multiplicity"], "0")


    def test_resolves_enum_ref_and_expands_composite_payload_shorthand(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        resolved = model.resolve_enum_value(
            "EnumRef.ВидыПлатежейВГосБюджет",
            "Налог",
            field="СубконтоДт1",
        )

        self.assertEqual(resolved["xmlType"], "EnumRef.ВидыПлатежейВГосБюджет")
        self.assertEqual(resolved["odataType"], "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет")
        self.assertTrue(resolved["metadataVerified"])
        self.assertTrue(resolved["memberVerified"])
        self.assertEqual(resolved["propertyCompatibility"], "unknown")
        self.assertEqual(
            resolved["payloadFragment"],
            {
                "СубконтоДт1": "Налог",
                "СубконтоДт1_Type": "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет",
            },
        )

        payload = {
            "Lines": [
                {
                    "LineNumber": 1,
                    "СубконтоДт1": {
                        "onec83ValueType": "EnumRef",
                        "enum": "ВидыПлатежейВГосБюджет",
                        "member": "Налог",
                    },
                }
            ]
        }
        normalized = model.normalize_payload("Document_Order", payload)

        self.assertEqual(normalized["Lines"][0]["LineNumber"], "1")
        self.assertEqual(normalized["Lines"][0]["СубконтоДт1"], "Налог")
        self.assertEqual(
            normalized["Lines"][0]["СубконтоДт1_Type"],
            "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет",
        )
        compatibility = model.enum_property_compatibility("Document_Order", normalized)
        self.assertEqual(len(compatibility), 1)
        self.assertEqual(compatibility[0]["path"], "Document_Order.Lines[0].СубконтоДт1")
        self.assertTrue(compatibility[0]["enumMetadataVerified"])
        self.assertTrue(compatibility[0]["memberMetadataVerified"])
        self.assertEqual(compatibility[0]["propertyCompatibility"], "unknown")

        row_description = model.describe_entity_set("Document_Order_Lines")
        properties = {item["name"]: item for item in row_description["properties"]}
        self.assertEqual(
            properties["СубконтоДт1"]["compositeTypeProperty"],
            "СубконтоДт1_Type",
        )
        self.assertEqual(properties["СубконтоДт1_Type"]["dispatchFor"], "СубконтоДт1")
        self.assertEqual(properties["СубконтоДт1_Type"]["allowedTypeStatus"], "unknown")
        self.assertIsNone(properties["СубконтоДт1_Type"]["allowedRuntimeTypes"])

    def test_owner_observed_enum_dispatch_is_limited_to_exact_test_context(self) -> None:
        model = MetadataModel.from_xml(CSDL)
        account_context = "synthetic_context"
        model.enum_context_mappings[("ВидыПлатежейВГосБюджет", "Налог", "СубконтоДт1", account_context)] = {"odataType": "StandardODATA.ВидыПлатежейВГосБюджет", "reason":"synthetic reviewed context", "evidence":"synthetic-review"}

        resolved = model.resolve_enum_value(
            "ВидыПлатежейВГосБюджет",
            "Налог",
            field="СубконтоДт1",
            account_context=account_context,
        )
        self.assertEqual(
            resolved["odataType"],
            "StandardODATA.ВидыПлатежейВГосБюджет",
        )
        self.assertEqual(resolved["odataTypeCandidates"][0], resolved["odataType"])
        self.assertEqual(resolved["propertyCompatibility"], "observed_for_context")
        self.assertEqual(resolved["accountContext"], account_context)
        self.assertIn("synthetic-review", resolved["propertyCompatibilityEvidence"])
        self.assertEqual(
            resolved["payloadFragment"],
            {
                "СубконтоДт1": "Налог",
                "СубконтоДт1_Type": "StandardODATA.ВидыПлатежейВГосБюджет",
            },
        )

        normalized = model.normalize_payload(
            "Document_Order",
            {
                "Lines": [{
                    "LineNumber": 1,
                    "СубконтоДт1": {
                        "onec83ValueType": "EnumRef",
                        "enum": "ВидыПлатежейВГосБюджет",
                        "member": "Налог",
                        "accountContext": account_context,
                    },
                }],
            },
        )
        self.assertEqual(
            normalized["Lines"][0]["СубконтоДт1_Type"],
            "StandardODATA.ВидыПлатежейВГосБюджет",
        )

        compatibility = model.enum_property_compatibility("Document_Order", {
            "Lines": [{
                "LineNumber": 1,
                "СубконтоДт1": "Налог",
                "СубконтоДт1_Type": "StandardODATA.ВидыПлатежейВГосБюджет",
            }],
        })
        self.assertEqual(len(compatibility), 1)
        self.assertTrue(compatibility[0]["enumMetadataVerified"])
        self.assertTrue(compatibility[0]["memberMetadataVerified"])
        self.assertEqual(compatibility[0]["propertyCompatibility"], "unknown")

        for wrong in (
            {"field": "СубконтоДт2", "account_context": account_context},
            {"field": "СубконтоДт1", "account_context": "other_context"},
            {"field": "СубконтоДт1", "account_context": account_context, "member": "Пени"},
        ):
            with self.assertRaisesRegex(ValueError, "no owner-confirmed"):
                model.resolve_enum_value(
                    "ВидыПлатежейВГосБюджет",
                    wrong.pop("member", "Налог"),
                    field=wrong["field"],
                    account_context=wrong["account_context"],
                )

    def test_valid_enum_member_is_rejected_by_explicit_incompatible_csdl_property(self) -> None:
        restricted_csdl = CSDL.replace(
            '<Property Name="СубконтоДт1_Type" Type="Edm.String" />',
            '<Property Name="СубконтоДт1_Type" Type="Edm.Guid" />',
        )
        model = MetadataModel.from_xml(restricted_csdl)

        resolved = model.resolve_enum_value(
            "ВидыПлатежейВГосБюджет",
            "Налог",
            field="СубконтоДт1",
        )
        self.assertTrue(resolved["metadataVerified"])
        self.assertTrue(resolved["memberVerified"])

        with self.assertRaisesRegex(ValueError, "not an OData dispatch string"):
            model.normalize_payload(
                "Document_Order",
                {
                    "Lines": [{
                        "LineNumber": 1,
                        "СубконтоДт1": {
                            "onec83ValueType": "EnumRef",
                            "enum": "ВидыПлатежейВГосБюджет",
                            "member": "Налог",
                        },
                    }],
                },
            )

    def test_rejects_unknown_enum_member_and_conflicting_type(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        with self.assertRaisesRegex(ValueError, "not declared"):
            model.resolve_enum_value("ВидыПлатежейВГосБюджет", "Штраф")

        with self.assertRaisesRegex(ValueError, "conflicts"):
            model.normalize_payload(
                "Document_Order",
                {
                    "Lines": [
                        {
                            "LineNumber": 1,
                            "СубконтоДт1": {
                                "onec83ValueType": "EnumRef",
                                "enum": "ВидыПлатежейВГосБюджет",
                                "member": "Налог",
                            },
                            "СубконтоДт1_Type": "StandardODATA.Enum_ВидыПлатежейВГосБюджет",
                        }
                    ]
                },
            )
        with self.assertRaisesRegex(ValueError, "conflicts"):
            model.normalize_payload(
                "Document_Order",
                {
                    "Lines": [
                        {
                            "LineNumber": 1,
                            "СубконтоДт1": {
                                "onec83ValueType": "EnumRef",
                                "enum": "ВидыПлатежейВГосБюджет",
                                "member": "Налог",
                            },
                            "СубконтоДт1_Type": None,
                        }
                    ]
                },
            )

        explicit_same = model.normalize_payload(
            "Document_Order",
            {
                "Lines": [
                    {
                        "LineNumber": 1,
                        "СубконтоДт1_Type": "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет",
                        "СубконтоДт1": {
                            "onec83ValueType": "EnumRef",
                            "enum": "ВидыПлатежейВГосБюджет",
                            "member": "Налог",
                        },
                    }
                ]
            },
        )
        self.assertEqual(
            explicit_same["Lines"][0]["СубконтоДт1_Type"],
            "StandardODATA.EnumRef_ВидыПлатежейВГосБюджет",
        )

    def test_rejects_unknown_entity_set_and_unknown_fields(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        with self.assertRaisesRegex(ValueError, "unknown entity set"):
            model.validate_payload("Document", {})

        with self.assertRaisesRegex(ValueError, "unknown field"):
            model.validate_payload("Document_Order", {"Hacked": True})

        with self.assertRaisesRegex(ValueError, "Lines\\[0\\]\\.Unexpected"):
            model.validate_payload("Document_Order", {"Lines": [{"Unexpected": 1}]})

    def test_rejects_scalar_and_collection_shape_mismatches(self) -> None:
        model = MetadataModel.from_xml(CSDL)

        with self.assertRaisesRegex(ValueError, "Ref_Key"):
            model.validate_payload("Document_Order", {"Ref_Key": "not-a-guid"})

        with self.assertRaisesRegex(ValueError, "Lines must be a list"):
            model.validate_payload("Document_Order", {"Lines": {"LineNumber": 1}})

        with self.assertRaisesRegex(ValueError, "Posted must be Edm.Boolean"):
            model.validate_payload("Document_Order", {"Posted": "yes"})

        with self.assertRaisesRegex(ValueError, "LineNumber"):
            model.validate_payload("Document_Order", {"Lines": [{"LineNumber": True}]})

    def test_rejects_unsafe_xml_declarations(self) -> None:
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            MetadataModel.from_xml("<!DOCTYPE x><Schema />")


if __name__ == "__main__":
    unittest.main()


def test_string_max_length_checked_without_truncation_in_nested_and_collection_fields():
    import pytest
    xml = CSDL.replace('<Property Name="Description" Type="Edm.String" />',
                       '<Property Name="Description" Type="Edm.String" MaxLength="5" />').replace(
                       '<Property Name="Contact" Type="Edm.String" />',
                       '<Property Name="Contact" Type="Edm.String" MaxLength="3" />')
    model = MetadataModel.from_xml(xml)
    assert model.normalize_payload("Catalog_Customer", {"Description": "АБВГД"}) == {"Description": "АБВГД"}
    with pytest.raises(ValueError, match="MaxLength 5"):
        model.validate_payload("Catalog_Customer", {"Description": "АБВГДЕ"})
    with pytest.raises(ValueError, match="Contacts\\[0\\].Contact.*MaxLength 3"):
        model.normalize_payload("Catalog_Customer", {"Contacts": [{"LineNumber": 1, "Contact": "abcd"}]})
    model = MetadataModel.from_xml(xml.replace('MaxLength="5"', 'MaxLength="Max"'))
    model.validate_payload("Catalog_Customer", {"Description": "x" * 200})
    model = MetadataModel.from_xml(xml.replace('MaxLength="5"', 'MaxLength="unknown"'))
    with pytest.raises(ValueError, match="unsupported MaxLength"):
        model.validate_payload("Catalog_Customer", {"Description": "x"})
