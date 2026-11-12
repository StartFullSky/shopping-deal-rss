"""Read attributable prices from public search extracts, without fetching a page."""

import datetime
import re
import time
from decimal import Decimal

from deal_text import normalized, product_kind, quantity


PRICE_LABEL = r"(?:参考价|当前价格|当前价|到手价|券后价|券后|实付|现价|售价|报价|价格|全网比价)"
NUMBER = r"(?P<amount>\d{1,6}(?:\.\d{1,2})?)(?![\d.])"
PRICE_PATTERNS = (
    re.compile(r"[¥￥]\s*" + NUMBER + r"\s*(?:元)?\s*(?P<from>起)?"),
    re.compile(PRICE_LABEL + r"\s*[:：]?\s*" + NUMBER + r"\s*元\s*(?P<from>起)?"),
    re.compile(r"(?<![\d.])" + NUMBER + r"\s*元\s*(?P<from>起)"),
    re.compile(r"^\s*" + PRICE_LABEL + r"\s*[:：]?\s*" + NUMBER + r"\s*$"),
)
NON_CURRENT = re.compile(
    r"原价|划线价|历史|史低|曾经|上次|发售价|首发价|上市价|建议零售价|指导价|"
    r"优惠券|券额|立减|满\s*[¥￥]?\s*\d+.{0,4}减|返现|返卡|返红包|赠金|运费|定金|订金|分期|"
    r"推荐|相关商品|同品牌|同分类|同价位|相关文章|爆料|二手|租金|限量.*名"
)
SECTION_END = re.compile(
    r"(?im)^\s*(?:#{2,}\s|同(?:价位|品牌|分类).*商品|猜你喜欢|相关推荐|好价爆料|全网内容|商品口碑|规格参数)"
)
CONDITION = re.compile(r"88\s*VIP|PLUS|会员|首单|新人|需用券|券后|国补|补贴", re.I)


def search_title(value):
    value = normalized(value).split("【报价", 1)[0].split("【规格", 1)[0]
    return re.sub(r"\s*[-—]\s*什么值得买.*$", "", value).strip()


def source_date(value):
    """Keep a search-reported date distinct from a verified price update time."""
    match = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime.date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def main_excerpt(value):
    # Exa extracts may contain whole recommendation lists. Never attribute their
    # prices to the result's main product, even when that would improve coverage.
    text = str(value or "")
    boundary = SECTION_END.search(text)
    if boundary:
        text = text[:boundary.start()]
    return text[:3000]


def snippet_reference(record, max_age_seconds):
    """Return a display reference; an indexed extract is never checkout proof."""
    if record.get("type") != "product":
        return None
    captured = record.get("captured_at")
    if not captured or time.time() - captured > max_age_seconds:
        return None
    title = record["title"]
    if re.search(r"多规格|多款|多尺寸|规格可选", title):
        return None
    size = quantity(title, default=False)
    if size is None:
        if re.search(r"组合|礼盒|整箱|套装", title):
            return None
        size = quantity(title)
    excerpt = main_excerpt(record.get("snippet"))
    prices, seen = [], set()
    # Only a currency-bearing title or the primary excerpt can supply a quote.
    raw = record.get("raw_title", title) + "\n" + excerpt
    raw = re.sub(r"(?<=\d),(?=\d{3}(?:[^\d]|$))", "", raw)
    for clause in re.split(r"[\r\n。；;，,|]", raw):
        clause = normalized(clause)
        if not clause or NON_CURRENT.search(clause):
            continue
        title_kind, clause_kind = product_kind(title), product_kind(clause)
        if title_kind and clause_kind and title_kind != clause_kind:
            continue
        clause_size = quantity(clause, default=False)
        if clause_size and (clause_size["unit"] == size["unit"] or {clause_size["unit"], size["unit"]} <= {"g", "ml"}):
            if (clause_size["unit"], Decimal(clause_size["amount"])) != (size["unit"], Decimal(size["amount"])):
                continue
        if re.search(r"\d[\d.]*\s*[-~～至]\s*[¥￥]?\s*\d", clause):
            continue
        units = r"千克|公斤|毫升|kg|ml|斤|克|升|抽|片|粒|条|支|件|瓶|包|盒|袋|桶|套|个|杯|g|l"
        if re.search(r"(?:每\s*\d*\s*(?:" + units + r")|[/／]\s*(?:每|\d|" + units + r"))", clause, re.I):
            continue
        if re.search(r"(?:拍|买|下单|购买)\s*(?:[2-9]|\d{2,})\s*(?:件|瓶|包|盒|袋|桶|套)", clause):
            continue
        for pattern in PRICE_PATTERNS:
            for match in pattern.finditer(clause):
                trailing = clause[match.end():].lstrip()
                if re.match(r"[-~～至折%\d.]|(?:g|kg|ml|件|瓶|包|盒)(?:\b|装)", trailing, re.I):
                    continue
                amount = Decimal(match["amount"])
                if not Decimal(0) < amount <= Decimal("999999") or amount in seen:
                    continue
                seen.add(amount)
                prices.append({
                    "amount": str(amount), "member": True if re.search(r"88\s*VIP|PLUS|会员", clause, re.I) else None,
                    "mall": None, "basis": "search_snippet_from_price" if match.groupdict().get("from") else "search_snippet_price",
                    "from_price": bool(match.groupdict().get("from")), "conditions": CONDITION.findall(clause), "evidence": clause[:320],
                })
    if not prices:
        return None
    return {
        "title": title, "url": record["url"], "quantity": size, "prices": prices,
        "quote_status": "snippet_quoted", "evidence_type": "search_snippet", "captured_at": captured,
        "source_date": record.get("source_date"), "source_updated_at": None,
        "snippet": excerpt, "search_provider": record.get("search_provider"),
    }


def exa_records(text, canonical_wiki, canonical_catalog):
    records, seen = [], set()
    blocks = re.split(r"(?m)(?=^Title:\s*\S)", text)
    captured = time.time()
    for block in blocks:
        heading = re.match(r"Title:\s*(.+)\r?\nURL:\s*(\S+)", block)
        if not heading:
            continue
        url = canonical_wiki(heading[2]) or canonical_catalog(heading[2])
        if not url or url in seen:
            continue
        seen.add(url)
        published = re.search(r"(?m)^Published(?: Date)?:\s*(.*)", block)
        content = re.split(r"(?m)^(?:Highlights|Text|Content):\s*\n?", block, maxsplit=1)
        records.append({
            "title": search_title(heading[1]), "raw_title": heading[1], "url": url,
            "search_provider": "exa_free", "type": "product" if canonical_wiki(url) else "catalog",
            "snippet": content[1][:6000] if len(content) == 2 else "",
            "source_date": source_date(published[1]) if published else None,
            "captured_at": captured,
        })
    return records
