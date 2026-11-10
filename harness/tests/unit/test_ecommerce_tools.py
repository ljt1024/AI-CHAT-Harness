import httpx
import pytest

from agentforge.config import Settings
from agentforge.tools import ecommerce
from agentforge.tools.ecommerce import (ECOM_TOOL_MAP, ProductDetailTool, ProductSearchTool,
                                        EcomError, format_items, normalise)

# Trimmed from the catalogue's documented response: the field names are the
# contract, and a rename upstream is what these fixtures catch.
ITEM = {
    "goodsid": 12345,
    "itemid": "Example-AbC123",
    "title": "罗技 MX Master 3S 无线鼠标",
    "mainimage": "https://example.com/product.jpg",
    "itemlink": "https://item.taobao.com/item.htm?id=672567619741",
    "originalprice": 699.0,
    "price": 559.0,
    "couponamount": 140.0,
    "validuntil": "2026-12-31 23:59:59",
    "monthsales": 1200,
    "dailysales": 160,
}


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=ecommerce.BASE_URL, transport=httpx.MockTransport(handler))


def settings(token: str = "test-token") -> Settings:
    return Settings(_env_file=None, taokeyi_token=token)


def test_normalise_reads_the_paid_price_and_the_listing_price():
    record = normalise(ITEM)
    assert record["id"] == "Example-AbC123"
    assert record["price"] == 559.0
    assert record["original_price"] == 699.0
    assert record["coupon"] == 140.0
    assert record["sales"] == 1200
    assert record["url"].startswith("https://item.taobao.com/")


def test_normalise_survives_a_record_missing_half_its_fields():
    """A partial row must not raise: the catalogue documents -1 and "" for gaps."""
    record = normalise({"goodsid": 1, "title": "x", "price": 9.9, "couponamount": 0,
                        "monthsales": None, "originalprice": 0})
    assert record["price"] == 9.9
    assert record["sales"] == 0
    assert record["url"] == ""


def test_format_items_says_the_pool_is_not_the_whole_market_when_empty():
    text = format_items("冷门型号", [])
    assert "冷门型号" in text
    assert "commission" in text  # the model must not read an empty page as "unavailable"


def test_format_items_shows_the_coupon_and_the_earlier_price():
    text = format_items("鼠标", [normalise(ITEM)])
    assert "¥559.0" in text
    assert "was ¥699.0" in text
    assert "coupon ¥140.0" in text
    assert "1200 sold/month" in text
    assert "672567619741" in text


@pytest.mark.asyncio
async def test_search_without_a_token_explains_what_the_operator_must_do():
    tool = ProductSearchTool(settings(""), client=client_for(lambda request: httpx.Response(500)))
    result = await tool.execute({"keyword": "鼠标"})
    assert result.ok is False
    assert "TAOKEYI_TOKEN" in result.error
    assert "ask the user" in result.error


@pytest.mark.asyncio
async def test_search_sends_the_filters_and_reads_the_list():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"code": 0, "message": "成功",
                                         "data": {"items": [ITEM], "hasmore": False}})

    tool = ProductSearchTool(settings(), client=client_for(handler))
    result = await tool.execute({"keyword": "鼠标", "min_price": 100, "max_price": 800,
                                 "sort": "price_asc", "limit": 3})
    assert result.ok is True
    assert "罗技" in result.output
    assert "keyword=%E9%BC%A0%E6%A0%87" in seen["url"]
    assert "minprice=100" in seen["url"]
    assert "maxprice=800" in seen["url"]
    assert "sort=price_asc" in seen["url"]
    assert "size=3" in seen["url"]
    assert seen["auth"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_an_unknown_sort_falls_back_instead_of_failing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    tool = ProductSearchTool(settings(), client=client_for(handler))
    await tool.execute({"keyword": "x", "sort": "cheapest"})
    assert "sort=latest" in seen["url"]


@pytest.mark.asyncio
async def test_a_limit_above_the_cap_is_clamped_not_rejected():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    tool = ProductSearchTool(settings(), client=client_for(handler))
    await tool.execute({"keyword": "x", "limit": 999})
    assert f"size={ecommerce.MAX_RESULTS}" in seen["url"]


@pytest.mark.asyncio
async def test_inverted_price_bounds_are_refused_before_the_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made")

    tool = ProductSearchTool(settings(), client=client_for(handler))
    result = await tool.execute({"keyword": "x", "min_price": 900, "max_price": 100})
    assert result.ok is False
    assert "above max_price" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [
    (401, "rejected the token"),
    (403, "not verified"),
    (429, "rate limit"),
    (404, "no such product"),
    (503, "answered 503"),
])
async def test_each_status_becomes_advice_rather_than_a_stack_trace(status, expected):
    tool = ProductSearchTool(settings(), client=client_for(lambda request: httpx.Response(status)))
    result = await tool.execute({"keyword": "x"})
    assert result.ok is False
    assert expected in result.error


@pytest.mark.asyncio
async def test_a_transport_failure_is_named_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    tool = ProductSearchTool(settings(), client=client_for(handler))
    result = await tool.execute({"keyword": "x"})
    assert result.ok is False
    assert "ConnectError" in result.error


@pytest.mark.asyncio
async def test_a_business_error_code_is_surfaced():
    tool = ProductSearchTool(settings(), client=client_for(lambda request: httpx.Response(
        200, json={"code": 10001, "message": "参数无效"})))
    result = await tool.execute({"keyword": "x"})
    assert result.ok is False
    assert "参数无效" in result.error


@pytest.mark.asyncio
async def test_a_link_is_reduced_to_its_item_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"code": 0, "data": ITEM})

    tool = ProductDetailTool(settings(), client=client_for(handler))
    result = await tool.execute({"item": "https://item.taobao.com/item.htm?spm=a1&id=672567619741"})
    assert result.ok is True
    assert seen["path"].endswith("/672567619741")
    assert "罗技" in result.output


@pytest.mark.asyncio
async def test_a_pasted_link_without_an_id_is_refused_with_a_shape_that_works():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made")

    tool = ProductDetailTool(settings(), client=client_for(handler))
    result = await tool.execute({"item": "https://detail.tmall.com/some/path"})
    assert result.ok is False
    assert "item.htm?id=" in result.error


def test_both_tools_are_registered_under_the_names_agents_pin():
    assert set(ECOM_TOOL_MAP) == {"search_products", "get_product"}


def test_an_empty_token_is_not_a_credential():
    with pytest.raises(EcomError):
        ecommerce._token(settings("   "))
