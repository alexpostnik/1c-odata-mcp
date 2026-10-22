import { describe, expect, it } from "vitest";
import { snapshotConnection, snapshotTools } from "./helpers/metadata-snapshot.js";
import { parseMetadata } from "../src/odata/metadata.js";

describe("проверка возможностей OData", () => {
  it("показывает требования и независимую диагностику без попытки записи", () => {
    const tools = snapshotTools(snapshotConnection());
    expect(tools["read.system.odata_requirements"]).toBeDefined();
    expect(tools["read.system.odata_capabilities"]).toBeDefined();
    expect(tools["read.system.odata_refresh"]).toBeDefined();
  });

  it("сохраняет объявленные функции и их типы результата отдельно от корня регистра", () => {
    const meta = parseMetadata(`<Edmx><DataServices><Schema Namespace="S">
      <ComplexType Name="BalanceResult"><Property Name="Amount" Type="Edm.Decimal"/></ComplexType>
      <EntityContainer><FunctionImport Name="Balance" ReturnType="Collection(S.BalanceResult)" IsBindable="true">
      <Parameter Name="bindingParameter" Type="S.AccountingRegister_Test"/>
      <Parameter Name="Period" Type="Edm.DateTime"/></FunctionImport></EntityContainer>
      </Schema></DataServices></Edmx>`);
    expect(meta).toHaveProperty("functions");
    expect(Reflect.get(meta, "functions")).toEqual([
      expect.objectContaining({ name: "Balance", bindingType: "AccountingRegister_Test", properties: [expect.objectContaining({ name: "Amount", type: "Edm.Decimal" })] }),
    ]);
  });
});
