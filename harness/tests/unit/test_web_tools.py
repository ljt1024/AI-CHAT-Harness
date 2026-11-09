import re

import pytest

from agentforge.tools import web
from agentforge.tools.web import (WebError, WebFetchTool, WebSearchTool, _assert_public,
                                  _assert_reachable, format_results, html_to_text,
                                  parse_bing_rss, parse_so)

# Trimmed copies of real result pages. The markup is the contract: when an engine
# changes it, these fixtures are what tell us before a user does.
SO_HTML = """
<ul class="result">
<li class="res-list" data-result-type="richsum"><h3 class="res-title"><a
  href="https://www.so.com/link?m=b6FUORjJ" data-mdurl="https://m.gmw.cn/2026-01/04/content_1304290728.htm"
  target="_blank"><em>国足主教练</em>邵佳一正式亮相</a></h3><div class='res-rich'>
  <p></p><span class="g-c-gray">2026年1月4日 - </span><span class="res-list-summary"
  data-result-summary>1月4日,<em>国足主教练</em>邵佳一在媒体见面会现场。...</span></div></li>
<li class="res-list"><h3 class="res-title"><a href="https://www.so.com/link?m=next">新任中国男足国家队主教练</a></h3>
  <p class="res-desc">没有 data-mdurl 时退回 href。</p></li>
<li class="res-list"><div class="ai-card">an AI card with no heading</div></li>
</ul>
"""

BING_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>react 19 - 搜索</title>
<item><title>React</title><link>https://react.dev/</link>
<description>The library for web and native user interfaces</description></item>
<item><title>Quick Start</title><link>https://react.dev/learn</link>
<description>&lt;b&gt;React&lt;/b&gt; in 10 minutes</description></item>
</channel></rss>
"""


@pytest.fixture(autouse=True)
def clean_cache():
    web._cache.clear()
    yield
    web._cache.clear()


def test_parse_so_prefers_the_real_target_over_the_click_counter():
    results = parse_so(SO_HTML, 6)
    assert [item["title"] for item in results] == ["国足主教练邵佳一正式亮相", "新任中国男足国家队主教练"]
    assert results[0]["url"] == "https://m.gmw.cn/2026-01/04/content_1304290728.htm"
    assert results[0]["snippet"].startswith("1月4日,国足主教练邵佳一")
    # No data-mdurl: the counter link is all there is, and it still points outward.
    assert results[1]["url"] == "https://www.so.com/link?m=next"
    assert "退回 href" in results[1]["snippet"]


def test_parse_so_skips_blocks_without_a_heading():
    # Three blocks, but the third is an AI card; reporting it would be a dead link.
    assert len(parse_so(SO_HTML, 6)) == 2


def test_parse_so_respects_the_limit():
    assert len(parse_so(SO_HTML, 1)) == 1


def test_parse_bing_rss_reads_titles_links_and_descriptions():
    results = parse_bing_rss(BING_RSS, 6)
    assert [item["url"] for item in results] == ["https://react.dev/", "https://react.dev/learn"]
    assert results[1]["snippet"] == "React in 10 minutes"


def test_parse_bing_rss_survives_a_non_xml_body():
    # An error page comes back as HTML with a 200; it must read as "no results".
    assert parse_bing_rss("<html>blocked</html>", 6) == []


def test_format_results_says_so_when_empty():
    assert "No results" in format_results("query", [])


def test_relevant_drops_results_about_the_wrong_question():
    # Bing CN really does return this for "中国队 世预赛 最新战绩"; it reads as an
    # answer about 中国 and would be quoted as if it were one.
    junk = [{"title": "中华人民共和国_百度百科", "url": "https://baike.baidu.com/x",
             "snippet": "成立于1949年10月1日，位于亚洲东部。"}]
    assert web.relevant("中国队 世预赛 最新战绩", junk) == []


def test_relevant_keeps_rows_that_share_a_term():
    rows = [{"title": "国足世预赛统计", "url": "https://m.hupu.com/bbs/1",
             "snippet": "36强赛6战2胜2平2负"},
            {"title": "快回国官网", "url": "https://spam.example/", "snippet": "AI 配音"}]
    kept = web.relevant("国足 世预赛 最新战绩", rows)
    assert [item["url"] for item in kept] == ["https://m.hupu.com/bbs/1"]


async def test_search_asks_the_chinese_engine_first_for_a_chinese_query(monkeypatch):
    calls = []

    async def fake_so(query):
        calls.append("so")
        return [{"title": "国足主教练邵佳一", "url": "https://example.com/a", "snippet": "s"}]

    async def fake_bing(query):
        calls.append("bing")
        return []

    monkeypatch.setattr(web, "_so", fake_so)
    monkeypatch.setattr(web, "_bing", fake_bing)
    results = await web.search("国足 主教练", 3)
    assert calls == ["so"]
    assert results[0]["url"] == "https://example.com/a"


async def test_search_falls_back_to_the_other_engine(monkeypatch):
    calls = []

    async def empty(query):
        calls.append("bing")
        return []

    async def working(query):
        calls.append("so")
        return [{"title": "React 19 release notes", "url": "https://example.com/b",
                 "snippet": "s"}]

    monkeypatch.setattr(web, "_bing", empty)
    monkeypatch.setattr(web, "_so", working)
    assert await web.search("react 19", 3)


async def test_search_reports_a_failure_rather_than_an_empty_answer(monkeypatch):
    async def broken(query):
        raise WebError("DNS lookup failed")

    monkeypatch.setattr(web, "_bing", broken)
    monkeypatch.setattr(web, "_so", broken)
    with pytest.raises(WebError):
        await web.search("anything 2026", 3)


def test_html_to_text_drops_scripts_and_keeps_paragraphs():
    title, text = html_to_text(
        "<html><head><title>标题</title><style>p{color:red}</style></head>"
        "<body><p>第一段</p><script>alert(1)</script><div>第二段</div></body></html>")
    assert title == "标题"
    assert "第一段" in text and "第二段" in text
    assert "alert" not in text and "color:red" not in text
    # Block elements separate the sentences; how many blank lines is cosmetic.
    assert re.search(r"第一段\s+第二段", text)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x",
                                 "https://user:pw@example.com/"])
def test_assert_public_refuses_non_public_shapes(url):
    with pytest.raises(WebError):
        _assert_public(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:8000/",
                                 "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                                 "http://10.0.0.5/"])
async def test_assert_reachable_refuses_internal_addresses(url):
    # No network needed: these resolve locally and are rejected before any connect.
    with pytest.raises(WebError):
        await _assert_reachable(url)


async def test_fetch_reports_refusal_instead_of_failing_the_run():
    result = await WebFetchTool().execute({"url": "http://127.0.0.1:8000/health"})
    assert result.ok is False
    assert "not a public address" in (result.error or "")


async def test_search_needs_a_query():
    result = await WebSearchTool().execute({"query": "   "})
    assert result.ok is False


def test_both_tools_are_registered_by_name():
    assert set(web.WEB_TOOL_MAP) == {"web_search", "web_fetch"}
    assert web.WEB_TOOL_MAP["web_fetch"].schema["required"] == ["url"]
    assert web.WEB_TOOL_MAP["web_search"].permission == "auto"
