from __future__ import annotations

import json
import errno
from pathlib import Path
import ssl
import sys

import pytest
import requests
import urllib3


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from onec_odata_mcp.auth import CredentialError, KeychainCredentialStore
from onec_odata_mcp.client import (
    ODataAcceptedResponseError,
    ODataClient,
    ODataClientError,
    ODataHTTPError,
    ODataTransportError,
)
from onec_odata_mcp.config import ConfigError, RuntimeConfig


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str | None = None, headers=None):
        self.status_code = status_code
        self._payload = payload
        if text is None and payload is not None:
            text = json.dumps(payload)
        self.text = "" if text is None else text
        self.content = self.text.encode("utf-8")
        self.headers = dict(headers or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_worker_client_uses_its_own_session_and_reuses_cached_credential(monkeypatch):
    transports = []
    credential_reads = []

    def make_transport():
        transport = FakeTransport([])
        transports.append(transport)
        return transport

    def read_password(service, user):
        credential_reads.append((service, user))
        return "secret"

    monkeypatch.setattr(ODataClient, "_default_transport", staticmethod(make_transport))
    client = ODataClient(config(), password_provider=read_password)
    assert client._password_value() == "secret"

    worker = client.for_worker()

    assert len(transports) == 2
    assert worker._transport is not client._transport
    assert worker._password_value() == "secret"
    assert credential_reads == [("onec83-test", "odata-user")]


def config(**overrides):
    values = {
        "odata_base_url": "https://example.test/base/odata/standard.odata/",
        "odata_user": "odata-user",
        "keychain_service": "onec83-test",
        "enable_write": False,
        "write_base_url": None,
        "target_kind": None,
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def test_runtime_config_from_env_requires_keychain_and_no_password_env():
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env(
            {
                "ONEC83_ODATA_BASE_URL": "https://example.test/base/odata/standard.odata/",
                "ONEC83_ODATA_USER": "user",
                "ONEC83_KEYCHAIN_SERVICE": "svc",
                "ONEC83_ODATA_PASSWORD": "secret",
            }
        )


def test_runtime_config_write_enabled_only_when_base_matches():
    cfg = RuntimeConfig.from_env(
        {
            "ONEC83_ODATA_BASE_URL": "https://example.test/base/odata/standard.odata",
            "ONEC83_ODATA_USER": "user",
            "ONEC83_KEYCHAIN_SERVICE": "svc",
            "ONEC83_ENABLE_WRITE": "true",
            "ONEC83_WRITE_BASE_URL": "https://example.test/base/odata/standard.odata/",
            "ONEC83_TARGET_KIND": "test",
        }
    )

    assert cfg.odata_base_url == "https://example.test/base/odata/standard.odata/"
    assert cfg.write_enabled is True


def test_runtime_config_rejects_mismatched_write_base_when_enabled():
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env(
            {
                "ONEC83_ODATA_BASE_URL": "https://example.test/base/odata/standard.odata/",
                "ONEC83_ODATA_USER": "user",
                "ONEC83_KEYCHAIN_SERVICE": "svc",
                "ONEC83_ENABLE_WRITE": "true",
                "ONEC83_WRITE_BASE_URL": "https://other.test/base/odata/standard.odata/",
            }
        )


def test_runtime_config_requires_test_target_for_writes():
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env(
            {
                "ONEC83_ODATA_BASE_URL": "https://example.test/base/odata/standard.odata/",
                "ONEC83_ODATA_USER": "user",
                "ONEC83_KEYCHAIN_SERVICE": "svc",
                "ONEC83_ENABLE_WRITE": "true",
                "ONEC83_WRITE_BASE_URL": "https://example.test/base/odata/standard.odata/",
                "ONEC83_TARGET_KIND": "prod",
            }
        )


def test_keychain_store_uses_security_without_shell(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return type("Completed", (), {"returncode": 0, "stdout": "secret\n"})()

    monkeypatch.setattr("subprocess.run", fake_run)

    assert KeychainCredentialStore().password("svc", "user") == "secret"
    assert calls[0][0] == ["security", "find-generic-password", "-s", "svc", "-a", "user", "-w"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["capture_output"] is True


def test_keychain_store_sanitizes_missing_credential(monkeypatch):
    def fake_run(args, **kwargs):
        return type("Completed", (), {"returncode": 44, "stdout": ""})()

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(CredentialError) as exc:
        KeychainCredentialStore().password("svc", "user")
    assert "svc" not in str(exc.value)
    assert "user" not in str(exc.value)


def test_list_entities_normalizes_v3_and_sends_safe_request_options():
    transport = FakeTransport(
        [
            FakeResponse(
                200,
                {
                    "d": {
                        "results": [{"Ref_Key": "a"}],
                        "__count": "1",
                        "__next": "next",
                    }
                },
            )
        ]
    )
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    result = client.list_entities(
        "Catalog_Контрагенты",
        top=10,
        skip=20,
        filter="DeletionMark eq false",
        select="Ref_Key,Description",
        orderby="Description asc",
        expand="Товары",
    )

    assert result == {"value": [{"Ref_Key": "a"}], "count": 1, "next_link": "next"}
    method, url, kwargs = transport.calls[0]
    assert method == "GET"
    assert url == "https://example.test/base/odata/standard.odata/Catalog_%D0%9A%D0%BE%D0%BD%D1%82%D1%80%D0%B0%D0%B3%D0%B5%D0%BD%D1%82%D1%8B"
    assert kwargs["params"]["$top"] == 10
    assert kwargs["params"]["$skip"] == 20
    assert kwargs["params"]["$expand"] == "Товары"
    assert kwargs["auth"] == ("odata-user", "secret")
    assert kwargs["timeout"] == 20.0
    assert kwargs["verify"] is True
    assert kwargs["allow_redirects"] is False


def test_list_entities_normalizes_v4():
    transport = FakeTransport([FakeResponse(200, {"value": [{"Ref_Key": "b"}], "@odata.count": 1})])
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    assert client.list_entities("Catalog_Items", top=1, skip=0) == {
        "value": [{"Ref_Key": "b"}],
        "count": 1,
    }


def test_count_entities_parses_text_count():
    transport = FakeTransport([FakeResponse(200, text="42")])
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    assert client.count_entities("Document_Order") == 42
    assert transport.calls[0][1] == "https://example.test/base/odata/standard.odata/Document_Order/$count"
    assert transport.calls[0][2]["headers"]["Accept"] == "application/json, application/xml, */*"


def test_get_entity_uses_uuid_key_and_normalizes_v3_item():
    transport = FakeTransport([FakeResponse(200, {"d": {"Ref_Key": "00000000-0000-0000-0000-000000000001"}})])
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    result = client.get_entity("Catalog_Items", "00000000-0000-0000-0000-000000000001")

    assert result == {"Ref_Key": "00000000-0000-0000-0000-000000000001"}
    assert transport.calls[0][1].endswith("Catalog_Items(guid'00000000-0000-0000-0000-000000000001')")


def test_get_accounting_register_record_set_uses_named_recorder_key_and_type():
    transport = FakeTransport([FakeResponse(200, {"d": {"RecordSet": [{"LineNumber": "1"}]}})])
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    result = client.get_register_record_set(
        "AccountingRegister_Хозрасчетный",
        "11111111-2222-4333-8444-000000000001",
        "StandardODATA.Document_ПоступлениеНаРасчетныйСчет",
    )

    assert result == {"RecordSet": [{"LineNumber": "1"}]}
    assert transport.calls[0][0] == "GET"
    assert (
        "AccountingRegister_%D0%A5%D0%BE%D0%B7%D1%80%D0%B0%D1%81%D1%87%D0%B5%D1%82%D0%BD%D1%8B%D0%B9"
        "(Recorder=guid'11111111-2222-4333-8444-000000000001',"
        "Recorder_Type='StandardODATA.Document_%D0%9F%D0%BE%D1%81%D1%82%D1%83%D0%BF%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5"
        "%D0%9D%D0%B0%D0%A0%D0%B0%D1%81%D1%87%D0%B5%D1%82%D0%BD%D1%8B%D0%B9%D0%A1%D1%87%D0%B5%D1%82')"
    ) in transport.calls[0][1]


def test_write_methods_are_disabled_by_default():
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=FakeTransport([]))

    with pytest.raises(PermissionError):
        client.create_entity("Catalog_Items", {"Description": "new"})


def test_create_entity_does_not_inject_ref_key_by_default():
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    transport = FakeTransport([FakeResponse(201, {"d": {"LineNo": 1}})])
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    assert client.create_entity("InformationRegister_Record", {"LineNo": 1}) == {"LineNo": 1}
    assert transport.calls[0][2]["json"] == {"LineNo": 1}


def test_create_entity_optionally_injects_null_ref_key_and_rejects_non_null_ref_key():
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    transport = FakeTransport([FakeResponse(201, {"d": {"Ref_Key": "created"}})])
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    assert client.create_entity("Catalog_Items", {"Description": "new"}, null_ref_key=True) == {"Ref_Key": "created"}
    assert transport.calls[0][2]["json"] == {
        "Description": "new",
        "Ref_Key": "00000000-0000-0000-0000-000000000000",
    }

    with pytest.raises(ValueError):
        client.create_entity(
            "Catalog_Items",
            {"Ref_Key": "00000000-0000-0000-0000-000000000001"},
            null_ref_key=True,
        )


def test_create_entity_uses_location_key_when_success_body_has_no_ref_key():
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    key = "11111111-2222-4333-8444-000000000002"
    transport = FakeTransport(
        [
            FakeResponse(
                201,
                {"d": {"Description": "created"}},
                headers={"Location": f"https://example.test/base/odata/standard.odata/Catalog_Items(guid'{key}')"},
            )
        ]
    )
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    assert client.create_entity("Catalog_Items", {"Description": "new"}) == {
        "Description": "created",
        "Ref_Key": key,
    }


def test_create_ref_key_entity_with_success_body_missing_key_needs_reconciliation():
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    transport = FakeTransport([FakeResponse(201, {"d": {"Description": "created"}})])
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataAcceptedResponseError) as exc:
        client.create_entity("Catalog_Items", {"Description": "new"}, null_ref_key=True)
    assert exc.value.accepted is True
    assert exc.value.key is None


def test_create_entity_reports_accepted_response_error_for_invalid_201_json_with_location_key():
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/synthetic-base-for-tests-12345/odata/standard.odata/",
        odata_base_url="https://example.test/synthetic-base-for-tests-12345/odata/standard.odata/",
        target_kind="test",
    )
    key = "11111111-2222-4333-8444-000000000002"
    transport = FakeTransport(
        [
            FakeResponse(
                201,
                text="{invalid secret response",
                headers={"Location": f"https://example.test/synthetic-base-for-tests-12345/odata/standard.odata/Catalog_Items(guid'{key}')"},
            )
        ]
    )
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataAcceptedResponseError) as exc:
        client.create_entity("Catalog_Items", {"Description": "new"})

    error = exc.value
    assert error.accepted is True
    assert error.status_code == 201
    assert error.key == key
    assert key in str(error)
    assert "https://example.test" not in str(error)
    assert "synthetic-base-for-tests-12345" not in str(error)
    assert "secret" not in str(error)


def test_create_entity_reports_accepted_response_error_for_malformed_201_json_with_location_key():
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    key = "11111111-2222-4333-8444-000000000002"
    transport = FakeTransport(
        [
            FakeResponse(
                201,
                ["not", "an", "entity"],
                headers={"Location": f"https://example.test/base/odata/standard.odata/Catalog_Items(guid'{key}')"},
            )
        ]
    )
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataAcceptedResponseError) as exc:
        client.create_entity("Catalog_Items", {"Description": "new"})

    assert exc.value.accepted is True
    assert exc.value.status_code == 201
    assert exc.value.key == key


def test_create_patch_and_action_write_without_retry():
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    transport = FakeTransport(
        [
            FakeResponse(503, {"error": "temporary"}),
            FakeResponse(204),
            FakeResponse(200, {"d": {"ok": True}}),
            FakeResponse(200, {"d": {"ok": True}}),
        ]
    )
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataClientError):
        client.create_entity("Catalog_Items", {"Description": "new"})
    assert len(transport.calls) == 1

    assert (
        client.patch_entity(
            "Catalog_Items",
            "00000000-0000-0000-0000-000000000001",
            {"Description": "x"},
            if_match="W/\"42\"",
        )
        is None
    )
    assert transport.calls[1][0] == "PATCH"
    assert transport.calls[1][2]["json"] == {"Description": "x"}
    assert transport.calls[1][2]["headers"]["If-Match"] == "W/\"42\""

    assert (
        client.invoke_action(
            "Catalog_Items",
            "00000000-0000-0000-0000-000000000001",
            "Post",
            if_match="W/\"42\"",
        )
        == {"ok": True}
    )
    assert transport.calls[2][1].endswith("Catalog_Items(guid'00000000-0000-0000-0000-000000000001')/Post")
    assert transport.calls[2][2]["params"] == {"PostingModeOperational": "false"}
    assert transport.calls[2][2]["headers"]["If-Match"] == "W/\"42\""

    assert client.invoke_action("Catalog_Items", "00000000-0000-0000-0000-000000000001", "Unpost") == {"ok": True}
    assert transport.calls[3][1].endswith("Catalog_Items(guid'00000000-0000-0000-0000-000000000001')/Unpost")
    assert transport.calls[3][2]["params"] == {}


@pytest.mark.parametrize("method_name", ["patch", "post"])
def test_write_methods_report_accepted_response_error_for_invalid_2xx_json(method_name):
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    key = "00000000-0000-0000-0000-000000000001"
    transport = FakeTransport([FakeResponse(200, text="{invalid secret response")])
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataAcceptedResponseError) as exc:
        if method_name == "patch":
            client.patch_entity("Catalog_Items", key, {"Description": "new"})
        else:
            client.invoke_action("Catalog_Items", key, "Post")

    assert exc.value.accepted is True
    assert exc.value.status_code == 200
    assert exc.value.key == key
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "error_response",
    [
        pytest.param(FakeResponse(500), id="unstructured-500"),
        pytest.param(
            FakeResponse(
                500,
                {
                    "odata.error": {
                        "code": "-1, Microsoft.Data.OData.ODataException",
                        "message": {"value": "A different server-side OData error"},
                    }
                },
            ),
            id="different-structured-500",
        ),
    ],
)
def test_get_retries_transient_errors_only_for_get(error_response):
    transport = FakeTransport([RuntimeError("socket failed"), error_response, FakeResponse(200, {"value": []})])
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    assert client.list_entities("Catalog_Items", top=10, skip=0) == {"value": []}
    assert len(transport.calls) == 3


def test_get_does_not_retry_known_1c_where_filter_rejection():
    response = FakeResponse(
        500,
        {
            "odata.error": {
                "code": "-1, Microsoft.Data.OData.ODataException",
                "message": {"lang": "ru-RU", "value": 'Операция не разрешена в предложении "ГДЕ"'},
            }
        },
    )
    transport = FakeTransport([response, FakeResponse(200, {"value": []})])
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataHTTPError) as exc:
        client.list_entities("Catalog_Items", top=10, skip=0, filter="synthetic filter canary")

    assert exc.value.status_code == 500
    assert exc.value.code == "-1, Microsoft.Data.OData.ODataException"
    assert 'Операция не разрешена в предложении "ГДЕ"' in str(exc.value)
    assert len(transport.calls) == 1


def test_http_error_extracts_v4_json_and_sanitizes_secrets_url_and_payload():
    cfg = config(
        odata_base_url="https://example.test/synthetic-base-for-tests-12345/odata/standard.odata/",
        enable_write=True,
        write_base_url="https://example.test/synthetic-base-for-tests-12345/odata/standard.odata/",
        target_kind="test",
    )
    transport = FakeTransport(
        [
            FakeResponse(
                400,
                {
                    "error": {
                        "code": "BadRequest",
                        "message": (
                            "Cannot create Description=Buyer Ltd with Amount=424242 at "
                            "https://example.test/synthetic-base-for-tests-12345/odata/standard.odata/"
                            "Document_Order password=s3cr3t-password Authorization: Basic abc123"
                        ),
                    }
                },
                headers={"Content-Type": "application/json"},
            )
        ]
    )
    client = ODataClient(cfg, password_provider=lambda service, user: "s3cr3t-password", transport=transport)

    with pytest.raises(ODataHTTPError) as exc:
        client.create_entity("Document_Order", {"Description": "Buyer Ltd", "Amount": 424242})

    error = exc.value
    assert error.status_code == 400
    assert error.code == "BadRequest"
    assert error.definitive is True
    message = str(exc.value)
    assert message.startswith("OData HTTP 400 code BadRequest: Cannot create")
    assert "https://example.test" not in message
    assert "synthetic-base-for-tests-12345" not in message
    assert "s3cr3t-password" not in message
    assert "Basic abc123" not in message
    assert "Description" in message
    assert "Buyer Ltd" not in message
    assert "Amount" in message
    assert "424242" not in message


def test_http_error_extracts_v3_odata_json_but_keeps_500_uncertain():
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    transport = FakeTransport(
        [
            FakeResponse(
                500,
                {
                    "odata.error": {
                        "code": "-1, Microsoft.Data.OData.ODataException",
                        "message": {"lang": "ru-RU", "value": "Для организации на Март 2010 не заполнена учетная политика"},
                        "innererror": {
                            "message": "Проведение документа отменено",
                            "stacktrace": "Traceback raw internals",
                        },
                    }
                },
            )
        ]
    )
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataHTTPError) as exc:
        client.invoke_action(
            "Catalog_Items",
            "00000000-0000-0000-0000-000000000001",
            "Post",
        )

    error = exc.value
    assert error.status_code == 500
    assert error.code == "-1, Microsoft.Data.OData.ODataException"
    assert error.definitive is False
    assert "Для организации" in str(error)
    assert "Проведение документа отменено" in str(error)
    assert "Traceback" not in str(error)


def test_http_error_extracts_xml_odata_error():
    transport = FakeTransport(
        [
            FakeResponse(
                400,
                text=(
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<m:error xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
                    "<m:code>Validation</m:code>"
                    '<m:message xml:lang="ru-RU">Не заполнено обязательное поле</m:message>'
                    "<m:innererror><m:message>Документ не записан</m:message>"
                    "<m:stacktrace>raw stack</m:stacktrace></m:innererror>"
                    "</m:error>"
                ),
                headers={"Content-Type": "application/xml"},
            )
        ]
    )
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataHTTPError) as exc:
        client.list_entities("Catalog_Items", top=10, skip=0)

    error = exc.value
    assert error.status_code == 400
    assert error.code == "Validation"
    assert error.definitive is True
    assert "Не заполнено обязательное поле" in str(error)
    assert "Документ не записан" in str(error)
    assert "raw stack" not in str(error)


def test_http_error_uses_plain_text_without_stacktrace_or_headers():
    transport = FakeTransport(
        [
            FakeResponse(
                400,
                text=(
                    "Не указан расчетный счет\n"
                    "Authorization: Basic abc123\n"
                    "Traceback (most recent call last)\n"
                    'File "server.py", line 1\n'
                ),
                headers={"Content-Type": "text/plain"},
            )
        ]
    )
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataHTTPError) as exc:
        client.list_entities("Catalog_Items", top=10, skip=0)

    error = exc.value
    assert error.status_code == 400
    assert error.definitive is True
    assert "Не указан расчетный счет" in str(error)
    assert "Basic abc123" not in str(error)
    assert "Traceback" not in str(error)
    assert "server.py" not in str(error)


def test_http_error_extracts_known_1c_session_block_from_html():
    transport = FakeTransport(
        [
            FakeResponse(
                403,
                text=(
                    "<html><head><title>internal account 12345</title></head><body>"
                    "До сообщения: password=hidden-value. "
                    "Начало сеанса с информационной базой запрещено. "
                    "В данный момент база занята для обновления или архивации, "
                    "дождитесь окончания блокировки."
                    " После сообщения: client token abc."
                    "</body></html>"
                ),
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        ]
    )
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataHTTPError) as exc:
        client.list_entities("Catalog_Items", top=10, skip=0)

    error = exc.value
    assert error.status_code == 403
    assert error.definitive is True
    assert "Начало сеанса" in str(error)
    assert "занята для обновления или архивации" in str(error)
    assert "internal account" not in str(error)
    assert "hidden-value" not in str(error)
    assert "client token" not in str(error)
    assert "<html" not in str(error)


@pytest.mark.parametrize("status_code", [408, 429, 502, 503, 504])
def test_http_error_marks_retryable_or_gateway_status_not_definitive(status_code):
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    transport = FakeTransport([FakeResponse(status_code, text="<html><body>temporary</body></html>")])
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataHTTPError) as exc:
        client.create_entity("Catalog_Items", {"Description": "new"})

    assert exc.value.status_code == status_code
    assert exc.value.definitive is False
    assert "<html" not in str(exc.value)
    assert "temporary" not in str(exc.value)


class ConnectTimeout(Exception):
    pass


class ProxyError(Exception):
    pass


class SSLError(Exception):
    pass


@pytest.mark.parametrize(
    ("exception", "category"),
    [
        (ConnectTimeout("https://example.test/base password=s3cr3t"), "timeout"),
        (ProxyError("proxy leaked password=s3cr3t"), "proxy"),
        (SSLError("certificate leaked password=s3cr3t"), "tls"),
        (ConnectionError("socket leaked password=s3cr3t"), "connection"),
        (RuntimeError("raw leaked password=s3cr3t"), "transport"),
    ],
)
def test_transport_errors_are_classified_sanitized_and_not_retried_for_writes(exception, category):
    cfg = config(
        enable_write=True,
        write_base_url="https://example.test/base/odata/standard.odata/",
        target_kind="test",
    )
    transport = FakeTransport([exception])
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    with pytest.raises(ODataTransportError) as exc:
        client.create_entity("Catalog_Items", {"Description": "new"})

    assert exc.value.category == category
    assert exc.value.attempts == 1
    assert len(transport.calls) == 1
    assert "s3cr3t" not in str(exc.value)
    assert "https://example.test" not in str(exc.value)


def test_nested_tls_wrapper_classifies_certificate_without_exposing_wrapper_text():
    class SSLCertVerificationError(Exception):
        pass

    class MaxRetryError(Exception):
        pass

    class RequestsConnectionError(Exception):
        pass

    leaf = SSLCertVerificationError("secret-cert https://secret-host/path")
    retry = MaxRetryError("credential=secret", leaf)
    wrapped = RequestsConnectionError("payload-secret", retry)
    cfg = config(get_retries=2)
    transport = FakeTransport([wrapped, FakeResponse(200, {"value": []})])
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)

    assert client.list_entities("Catalog_Items", 1, 0) == {"value": []}
    assert len(transport.calls) == 2

    cfg = config(enable_write=True, write_base_url="https://example.test/base/odata/standard.odata/",
                 target_kind="test")
    writer = ODataClient(cfg, password_provider=lambda service, user: "secret",
                         transport=FakeTransport([wrapped]))
    with pytest.raises(ODataTransportError) as caught:
        writer.create_entity("Catalog_Items", {"Description": "payload-secret"})
    assert caught.value.category == "tls"
    assert caught.value.subreason == "tls_certificate"
    assert caught.value.attempts == 1
    assert all(canary not in str(caught.value) for canary in
               ("secret-cert", "secret-host", "credential=secret", "payload-secret"))


def test_wrapped_timeout_has_safe_subreason_and_write_stays_single_attempt():
    class ConnectTimeout(Exception):
        pass

    class MaxRetryError(Exception):
        pass

    class RequestsConnectionError(Exception):
        pass

    wrapped = RequestsConnectionError(
        "payload-canary", MaxRetryError("https://host-canary", ConnectTimeout("password-canary"))
    )
    cfg = config(enable_write=True, write_base_url="https://example.test/base/odata/standard.odata/",
                 target_kind="test")
    transport = FakeTransport([wrapped])
    client = ODataClient(cfg, password_provider=lambda service, user: "secret", transport=transport)
    with pytest.raises(ODataTransportError) as caught:
        client.create_entity("Catalog_Items", {"Description": "payload-canary"})
    assert caught.value.category == "timeout"
    assert caught.value.subreason == "timeout_connect"
    assert len(transport.calls) == 1
    assert all(canary not in str(caught.value) for canary in
               ("payload-canary", "host-canary", "password-canary"))


def test_actual_requests_urllib3_certificate_wrapper_is_structurally_classified():
    leaf = ssl.SSLCertVerificationError("certificate-canary https://host-canary.invalid")
    retry = urllib3.exceptions.MaxRetryError(
        None, "https://host-canary.invalid/path?token=canary", reason=leaf
    )
    wrapped = requests.exceptions.SSLError(retry)
    cfg = config(enable_write=True, write_base_url="https://example.test/base/odata/standard.odata/",
                 target_kind="test")
    transport = FakeTransport([wrapped])
    client = ODataClient(cfg, password_provider=lambda service, user: "password-canary",
                         transport=transport)

    with pytest.raises(ODataTransportError) as caught:
        client.create_entity("Catalog_Items", {"Description": "payload-canary"})
    assert (caught.value.category, caught.value.subreason, caught.value.attempts) == (
        "tls", "tls_certificate", 1
    )
    assert all(canary not in str(caught.value) for canary in
               ("certificate-canary", "host-canary", "token=canary", "payload-canary"))
    assert len(transport.calls) == 1


def test_generic_ssl_text_is_not_promoted_and_cyclic_args_are_bounded():
    generic_ssl = requests.exceptions.SSLError("TLSV1_ALERT_HANDSHAKE_FAILURE secret-canary")
    assert ODataTransportError("tls", attempts=1, subreason={"secret": "value"}).subreason is None

    cycle: list[object] = []
    cycle.append(cycle)
    wrapped = requests.exceptions.ConnectionError()
    wrapped.args = (cycle,)
    cfg = config(enable_write=True, write_base_url="https://example.test/base/odata/standard.odata/",
                 target_kind="test")
    client = ODataClient(cfg, password_provider=lambda service, user: "secret",
                         transport=FakeTransport([generic_ssl]))
    with pytest.raises(ODataTransportError) as caught:
        client.create_entity("Catalog_Items", {"Description": "new"})
    assert caught.value.category == "tls"
    assert caught.value.subreason is None

    client._transport = FakeTransport([wrapped])
    with pytest.raises(ODataTransportError) as caught:
        client.create_entity("Catalog_Items", {"Description": "new"})
    assert caught.value.category == "connection"
    assert caught.value.subreason is None


@pytest.mark.parametrize(
    ("exception", "subreason"),
    [
        (requests.exceptions.ConnectTimeout("host-canary.invalid"), "timeout_connect"),
        (requests.exceptions.ReadTimeout("secret-canary"), "timeout_read"),
    ],
)
def test_actual_requests_timeout_types_get_finite_safe_subreasons(exception, subreason):
    cfg = config(enable_write=True, write_base_url="https://example.test/base/odata/standard.odata/",
                 target_kind="test")
    client = ODataClient(cfg, password_provider=lambda service, user: "secret",
                         transport=FakeTransport([exception]))
    with pytest.raises(ODataTransportError) as caught:
        client.create_entity("Catalog_Items", {"Description": "payload-canary"})
    assert caught.value.category == "timeout"
    assert caught.value.subreason == subreason
    assert caught.value.attempts == 1
    assert all(canary not in str(caught.value) for canary in ("host-canary", "secret-canary", "payload-canary"))


def test_urllib3_name_resolution_inheritance_is_not_misreported_as_timeout():
    dns_error = urllib3.exceptions.NameResolutionError(
        "dns-canary.invalid", None, OSError("dns-secret")
    )
    cfg = config(enable_write=True, write_base_url="https://example.test/base/odata/standard.odata/",
                 target_kind="test")
    client = ODataClient(cfg, password_provider=lambda service, user: "secret",
                         transport=FakeTransport([dns_error]))
    with pytest.raises(ODataTransportError) as caught:
        client.create_entity("Catalog_Items", {"Description": "payload-canary"})
    assert caught.value.category == "connection"
    assert caught.value.subreason == "connection_dns"
    assert caught.value.attempts == 1

    refused = urllib3.exceptions.NewConnectionError(None, "refused-canary.invalid")
    refused.errno = errno.ECONNREFUSED
    client._transport = FakeTransport([refused])
    with pytest.raises(ODataTransportError) as caught:
        client.create_entity("Catalog_Items", {"Description": "payload-canary"})
    assert caught.value.category == "connection"
    assert caught.value.subreason == "connection_refused"


@pytest.mark.parametrize(
    "entity_set",
    ["../Catalog_Items", "Catalog Items", "Catalog_Items/$count", "Catalog_Items?x=1"],
)
def test_rejects_unsafe_path_names(entity_set):
    client = ODataClient(config(), password_provider=lambda service, user: "secret", transport=FakeTransport([]))

    with pytest.raises(ValueError):
        client.count_entities(entity_set)
