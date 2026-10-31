"""The commerce adapter family — registry, catalogue, credentials, base helpers,
the mock, the contract helper and the fake-commerce harness (decisions §273).

No database. Provider test modules call the same contract
(`tests/commerce_contract.py`) the mock is held to here.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from decimal import Decimal

import httpx
import pytest

from app.config import Settings, settings
from app.services.commerce_adapters import (
    CommerceAuthError,
    CommerceConfigError,
    CommerceRateLimitedError,
    CommerceRejectedError,
    CommerceTransientError,
    InventoryAdjustment,
    InventoryAdjustmentResult,
    UnknownCommerceAdapterError,
    catalog,
    get_commerce_adapter,
    list_available_adapters,
    mock_adapter,
    registry,
)
from app.services.commerce_adapters import base as commerce_base
from app.services.commerce_adapters.base import (
    CommerceAdapter,
    CommerceProviderSpec,
    http_failure,
    require_integral,
    transport_failure,
)
from tests.commerce_contract import (
    ContractCase,
    assert_commerce_contract,
    load_fake_commerce,
    use_fake_commerce,
)

# The English main catalogue plus the settings slice, which /organization loads
# and which holds the commerce form's labels (decisions §300).
_LOCALES = pathlib.Path(__file__).resolve().parents[2] / "frontend/src/lib/i18n/locales"
EN_CATALOGUES = (_LOCALES / "en.ts", _LOCALES / "settings/en.ts")


@pytest.fixture(autouse=True)
def _fresh_mock():
    mock_adapter.reset()
    yield
    mock_adapter.reset()


class _SecretAdapter(CommerceAdapter):
    """A throwaway provider with a secret and a destination field."""

    async def test_connection(self) -> None:
        return None

    async def list_locations(self):
        return []

    async def resolve_sku(self, sku):
        raise NotImplementedError

    async def adjust_available(self, adjustment):
        raise NotImplementedError


_SECRET_SPEC = CommerceProviderSpec(
    key="secretshop",
    label="Secret Shop",
    auth="credentials",
    fields=(
        catalog.field("shop_domain"),
        catalog.field("access_token", secret=True),
        catalog.field("api_version", required=False),
    ),
    docs_url="https://example.invalid/docs",
)


@pytest.fixture
def secret_provider(monkeypatch):
    """Register `_SecretAdapter` for one test without leaking it into the registry."""
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry.registered_adapters()))
    registry.register_commerce_adapter("secretshop", _SECRET_SPEC)(_SecretAdapter)
    yield _SecretAdapter


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_mock_is_discovered_without_an_import_list():
    assert "mock" in list_available_adapters()


def test_an_unknown_provider_fails_closed_never_mock():
    with pytest.raises(UnknownCommerceAdapterError) as err:
        get_commerce_adapter({"provider": "shopifyy"})
    assert err.value.provider == "shopifyy"
    with pytest.raises(UnknownCommerceAdapterError):
        get_commerce_adapter({})


def test_every_provider_registers_in_the_shape_the_register_guard_reads():
    """`pnpm check:subprocessors` finds providers by a column-0
    `@register_<family>_adapter("<slug>"` line; a key passed only inside the spec
    would hide a provider that receives tenant data from the register check."""
    import inspect

    for key, cls in registry.registered_adapters().items():
        source = inspect.getsource(inspect.getmodule(cls))
        assert re.search(rf'^@register_commerce_adapter\(\s*"{key}"', source, re.M), (
            f'{cls.__module__} must register as @register_commerce_adapter("{key}", SPEC)'
        )


def test_the_decorator_key_must_match_the_spec():
    with pytest.raises(RuntimeError, match="does not match"):
        registry.register_commerce_adapter("other", _SECRET_SPEC)


def test_two_modules_cannot_claim_one_provider(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry.registered_adapters()))

    class Other(_SecretAdapter):
        pass

    with pytest.raises(RuntimeError, match="registered twice"):
        registry.register_commerce_adapter("mock", dataclasses.replace(_SECRET_SPEC, key="mock"))(
            Other
        )


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


def test_every_registered_spec_is_well_formed():
    for key, cls in registry.registered_adapters().items():
        spec = cls.spec
        assert spec.key == key
        assert re.fullmatch(r"[a-z][a-z0-9_]*", key), key
        assert spec.auth in ("credentials", "oauth")
        assert spec.label and spec.docs_url.startswith("https://")
        names = [f["name"] for f in spec.fields]
        assert len(names) == len(set(names)), f"{key}: duplicate field"
        for f in spec.fields:
            assert set(f) >= {"name", "label_key", "secret", "required"}
            assert f["name"] not in ("provider", "location_id", "entity_locations"), (
                f"{key}: '{f['name']}' is reserved by settings.commerce"
            )


def test_every_catalogue_message_key_exists_in_the_frontend_catalogue():
    text = "".join(p.read_text() for p in EN_CATALOGUES)
    keys = {f"org.commerce.field.{name}" for name in catalog.KNOWN_FIELD_LABELS}
    for cls in registry.registered_adapters().values():
        for f in cls.spec.fields:
            keys.add(f["label_key"])
            if "help_key" in f:
                keys.add(f["help_key"])
    missing = sorted(k for k in keys if f"'{k}':" not in text)
    assert not missing, f"the frontend English catalogue (main + settings slice) lacks {missing}"


def test_every_host_naming_field_is_a_destination_key():
    for cls in registry.registered_adapters().values():
        for f in cls.spec.fields:
            name = f["name"]
            if name.endswith("_url") or "domain" in name or "host" in name:
                assert name in catalog.destination_keys(), (
                    f"{cls.spec.key}.{name} names where credentials go; add it to the "
                    "spec's destination_keys"
                )


def test_settings_commerce_holds_configuration_and_a_save_drops_secrets_on_a_move(
    secret_provider,
):
    stored = {
        "provider": "secretshop",
        "shop_domain": "a.example.com",
        "location_id": "loc-1",
    }
    # A read never shows a secret, even a stray copy left in the JSONB.
    public = catalog.public_commerce_config({**stored, "access_token": "tok-123"})
    assert "access_token" not in public
    assert public["location_id"] == "loc-1"

    same = catalog.merge_commerce_update(
        stored, {"provider": "secretshop", "shop_domain": "a.example.com", "access_token": ""}
    )
    assert "access_token" not in same  # secrets are never configuration
    # The location mapping is not a form field, so a connection save keeps it.
    assert same["location_id"] == "loc-1"
    assert catalog.secrets_to_drop(stored, same) == frozenset()

    moved = catalog.merge_commerce_update(
        stored, {"provider": "secretshop", "shop_domain": "evil.example.com"}
    )
    assert "access_token" in catalog.secrets_to_drop(stored, moved)

    switched = catalog.merge_commerce_update(stored, {"provider": "mock"})
    assert "location_id" not in switched  # another platform's location ids mean nothing
    assert "access_token" in catalog.secrets_to_drop(stored, switched)

    # Secrets entered before the first configuration save belong to it.
    assert catalog.secrets_to_drop({}, same) == frozenset()


def test_every_provider_secret_is_a_sealed_store_field():
    """A spec secret missing from `SECRET_FIELDS["commerce"]` could be neither
    written (the credentials endpoint refuses an unknown path) nor kept out of
    the JSONB — the commerce twin of `test_erp_catalog.py`'s guard."""
    from app.services.provider_credentials import CREDENTIAL_BLOCKS, SECRET_FIELDS

    assert "commerce" in CREDENTIAL_BLOCKS
    missing = sorted(catalog.secret_keys() - SECRET_FIELDS["commerce"])
    assert not missing, f'add {missing} to provider_credentials.SECRET_FIELDS["commerce"]'
    # And every sealed name is a registered provider's secret: a stale entry
    # would let the endpoint store a value no adapter reads.
    stale = sorted(SECRET_FIELDS["commerce"] - catalog.secret_keys())
    assert not stale, f'{stale} in SECRET_FIELDS["commerce"] belong to no registered provider'


def test_location_for_prefers_the_entity_mapping():
    config = {"location_id": "main", "entity_locations": {"e-1": "warehouse"}}
    assert catalog.location_for(config, "e-1") == "warehouse"
    assert catalog.location_for(config, "e-2") == "main"
    assert catalog.location_for(config, None) == "main"
    assert catalog.location_for({"entity_locations": {"e-1": " "}}, "e-1") is None


def test_the_adapter_is_built_from_the_resolved_config(secret_provider):
    resolved = {"provider": "secretshop", "shop_domain": "a.example.com", "access_token": "tok"}
    assert get_commerce_adapter(resolved).config == resolved


# --------------------------------------------------------------------------- #
# Base helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, CommerceAuthError),
        (403, CommerceAuthError),
        (429, CommerceRateLimitedError),
        (500, CommerceTransientError),
        (503, CommerceTransientError),
        (408, CommerceTransientError),
        (400, CommerceRejectedError),
        (404, CommerceRejectedError),
    ],
)
def test_http_failure_maps_by_status_and_never_copies_the_body(status, error):
    resp = httpx.Response(
        status, text="secret-token-in-body", request=httpx.Request("GET", "https://x")
    )
    exc = http_failure("adjust", resp)
    assert type(exc) is error
    assert "secret-token-in-body" not in str(exc)
    assert exc.retryable is (error in (CommerceTransientError, CommerceRateLimitedError))


def test_transport_failure_names_only_the_class():
    exc = transport_failure("list", httpx.ConnectError("https://shop/?token=abc"))
    assert "abc" not in str(exc) and "ConnectError" in str(exc)
    assert exc.retryable


def test_require_integral():
    assert require_integral(Decimal("3.000")) == 3
    with pytest.raises(CommerceRejectedError) as err:
        require_integral(Decimal("2.5"))
    assert err.value.code == "fractional_quantity" and not err.value.retryable


async def test_api_base_uses_the_override_per_provider(secret_provider, monkeypatch):
    monkeypatch.setattr(settings, "commerce_api_base_override", "http://localhost:12113/")
    adapter = secret_provider({"provider": "secretshop"})
    assert await adapter.api_base("https://shop.example.com") == "http://localhost:12113/secretshop"


async def test_api_base_refuses_plain_http_and_internal_hosts(secret_provider, monkeypatch):
    monkeypatch.setattr(settings, "commerce_api_base_override", "")
    adapter = secret_provider({"provider": "secretshop"})
    with pytest.raises(CommerceConfigError):
        await adapter.api_base("http://shop.example.com")
    with pytest.raises(CommerceConfigError):
        await adapter.api_base("https://127.0.0.1")
    with pytest.raises(CommerceConfigError):
        await adapter.api_base("https://169.254.169.254")


async def test_http_client_is_pinned_and_a_rebind_is_a_config_error(secret_provider, monkeypatch):
    """`api_base`'s check resolves once; httpx would resolve again at connect.
    Outside the overrides the client is pinned, and an internal answer at
    connect time is the same CommerceConfigError the check raises."""
    import httpcore

    from app.utils import url_safety

    monkeypatch.setattr(settings, "commerce_api_base_override", "")
    monkeypatch.setattr(commerce_base, "_TRANSPORT_OVERRIDE", None)

    class _Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, *_a, **_kw):
            raise AssertionError("an internal answer must not be dialled")

        async def connect_unix_socket(self, *_a, **_kw):
            raise AssertionError("never a unix socket")

        async def sleep(self, seconds):
            pass

    async def lookup(host, port):
        return ["169.254.169.254"]

    monkeypatch.setattr(url_safety.httpcore, "AnyIOBackend", _Backend)
    monkeypatch.setattr(url_safety, "_lookup_async", lookup)
    adapter = secret_provider({"provider": "secretshop"})
    async with adapter.http_client() as client:
        assert isinstance(client._transport, url_safety.PinnedAsyncTransport)
        with pytest.raises(CommerceConfigError):
            await client.get("https://shop.example.com/admin/api/locations.json")

    monkeypatch.setattr(settings, "commerce_api_base_override", "http://localhost:12113")
    async with adapter.http_client() as client:
        assert not isinstance(client._transport, url_safety.PinnedAsyncTransport)


def test_require_names_the_missing_field(secret_provider):
    with pytest.raises(CommerceConfigError, match="access_token"):
        secret_provider({"provider": "secretshop"}).require("access_token")


def test_a_deployed_environment_refuses_the_override():
    with pytest.raises(ValueError, match="FEOH_COMMERCE_API_BASE_OVERRIDE"):
        Settings(
            environment="production",
            commerce_api_base_override="http://localhost:12113",
            secret_key="x" * 40,
            hcaptcha_secret="h",
            hcaptcha_sitekey="site-key",
            credential_kms_key_id="arn:aws:kms:us-east-1:111122223333:key/test",
        )


# --------------------------------------------------------------------------- #
# The mock, held to the contract
# --------------------------------------------------------------------------- #


async def _mock_available(item_id: str, location_id: str) -> Decimal:
    return mock_adapter.available(item_id, location_id)


async def test_mock_honours_the_contract():
    await assert_commerce_contract(
        ContractCase(
            provider="mock",
            config={"provider": "mock"},
            known_sku="WPS-97",
            unmapped_sku="UNMAPPED-1",
            location_id="mock-main",
            unknown_location_id="nowhere",
            read_available=_mock_available,
        )
    )


async def test_the_contract_catches_an_adapter_that_double_applies(monkeypatch):
    """The helper is only worth calling if it fails a broken adapter."""

    class Leaky(mock_adapter.MockCommerceAdapter):
        async def adjust_available(self, adjustment):
            mock_adapter._applied.pop(adjustment.idempotency_key, None)
            return await super().adjust_available(adjustment)

    monkeypatch.setattr(registry, "_REGISTRY", {**registry.registered_adapters(), "mock": Leaky})
    with pytest.raises(AssertionError, match="replayed"):
        await assert_commerce_contract(
            ContractCase(
                provider="mock",
                config={"provider": "mock"},
                known_sku="WPS-97",
                unmapped_sku="UNMAPPED-1",
                location_id="mock-main",
                unknown_location_id="nowhere",
                read_available=_mock_available,
            )
        )


async def test_mock_transient_sku_is_retryable():
    adapter = get_commerce_adapter({"provider": "mock"})
    with pytest.raises(CommerceTransientError):
        await adapter.adjust_available(
            InventoryAdjustment(
                item_id="mock-item-TRANSIENT-1",
                location_id="mock-main",
                delta=Decimal("1"),
                idempotency_key="k",
                sku="TRANSIENT-1",
            )
        )


# --------------------------------------------------------------------------- #
# fake-commerce
# --------------------------------------------------------------------------- #


async def test_fake_commerce_mounts_providers_and_resets(monkeypatch):
    fake = load_fake_commerce()
    assert set(fake.PROVIDERS) == {p.stem for p in _provider_files()}
    with use_fake_commerce(monkeypatch):
        assert settings.commerce_api_base_override == "http://fake-commerce"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake.app), base_url="http://fake-commerce"
        ) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            assert (await client.post("/__reset")).json() == {"status": "reset"}
            assert (await client.get("/__providers")).json() == {
                "providers": sorted(fake.PROVIDERS)
            }
            assert (await client.get("/__state/nope")).status_code == 404
    assert commerce_base._TRANSPORT_OVERRIDE is None


def _provider_files():
    root = pathlib.Path(__file__).resolve().parents[2] / "tools/fake-commerce/providers"
    return [p for p in root.glob("*.py") if not p.name.startswith("_")]


def test_inventory_book_is_idempotent_and_whole_unit():
    import sys

    sys.modules.pop("providers._inventory", None)
    load_fake_commerce()
    from providers._inventory import InventoryBook  # type: ignore[import-not-found]

    book = InventoryBook(items={"SKU-1": "item-1"}, locations={"loc-1": "Main"})
    first, replayed = book.adjust("item-1", "loc-1", Decimal("3"), "k1")
    assert not replayed and first.available_after == 3
    again, replayed = book.adjust("item-1", "loc-1", Decimal("3"), "k1")
    assert replayed and again.reference == first.reference
    assert book.available("item-1", "loc-1") == 3
    with pytest.raises(KeyError):
        book.adjust("item-1", "loc-9", Decimal("1"), "k2")
    with pytest.raises(ValueError):
        book.adjust("item-1", "loc-1", Decimal("0.5"), "k3")


def test_result_reference_is_required_shape():
    assert InventoryAdjustmentResult(provider_reference="r").replayed is False
