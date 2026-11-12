import datetime
import html
import re
import unicodedata
from decimal import Decimal


MEASURE = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*(?P<u>千克|公斤|毫升|ml(?![a-z])|kg(?![a-z])|斤|克|升|g(?![a-z])|l(?![a-z])|抽|张|片|粒|条|支|盒|瓶|袋|包|件|台|个|只|套|双|桶|杯)", re.I)
MULTIPLIER = re.compile(r"^\s*[*x×]\s*(\d+)\s*(提|包|袋|瓶|支|盒|件|个|只|套|双)?", re.I)
MONEY = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:元|亓|块|r(?![a-z]))(?![/每])", re.I)
PER_PRICE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:元|亓|块|r)?\s*(?:[/每]|一)\s*(提|瓶|袋|包|支|件|盒|桶|杯|斤|kg|粒|片|抽|张|双)", re.I)
PRODUCT_WORDS = ("棉柔巾", "洗脸巾", "白胡椒粉", "芝麻球", "洗衣液", "洗衣粉", "洗发水", "洗发露", "沐浴露", "洁面乳", "洗面奶", "牙膏", "牙刷", "面膜", "牛奶", "酸奶", "豆腐包", "粉丝包", "菜包", "肉包", "毛巾", "浴巾", "卫生巾", "湿巾", "抽纸", "纸尿裤", "成长裤", "食用油", "花生油", "面包", "饼干", "咖啡", "锅", "水杯", "洗地机", "吹风机", "电吹风", "耳机", "充电器", "移动硬盘", "扩展坞", "袜子", "羽绒服", "衬衫", "风衣", "保湿霜", "洗手液", "巧克力", "洗衣皂", "洁厕剂", "洗洁精", "洗衣凝珠", "柔顺剂", "洗烘套装", "洗烘一体机", "洗衣机", "干衣机", "冰箱", "冷柜", "面霜")
ALIASES = {"洗发露": "洗发水", "洗面奶": "洁面乳", "洗脸巾": "棉柔巾", "电吹风": "吹风机", "豆腐包": "包子", "粉丝包": "包子", "菜包": "包子", "肉包": "包子", "保湿霜": "面霜", "男表": "手表", "女表": "手表"}
PRODUCT_WORDS += ("手机", "平板电脑", "笔记本电脑", "电脑", "玩具", "运动鞋", "篮球鞋", "保温杯", "表带", "手表", "男表", "女表")
PROMO = re.compile(r"88vip|plus|移动端|百亿补贴|补贴后|限时|国补|领券|首单|新品|官方|旗舰店|优惠|包邮|到手|实付|仅需|低至|拍下|任选|买一送一", re.I)
UNIT_SCALE = {"g": ("g", 1), "克": ("g", 1), "kg": ("g", 1000), "公斤": ("g", 1000), "千克": ("g", 1000), "斤": ("g", 500), "ml": ("ml", 1), "毫升": ("ml", 1), "l": ("ml", 1000), "升": ("ml", 1000)}


def normalized(value):
    value = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", value.replace("\u200b", "")).strip()


def identity_title(title):
    title = normalized(title)
    title = re.sub(r"\d+(?:\.\d+)?\s*(?:元|亓|块|r)\s*(?:起|包邮)?\s*$", "", title, flags=re.I)
    title = re.sub(r"https?://\S+", "", title)
    return PROMO.sub(" ", title).strip(" :：,，!！")


def quantity(text, default=True):
    text = normalized(text).lower().replace("×", "*")
    text = re.sub(r"\d+\s*件套", "1套", text)
    matches = list(MEASURE.finditer(text))
    physical = []
    counted = []
    consumed_until = -1
    for match in matches:
        if match.start() < consumed_until:
            continue
        number = Decimal(match["n"])
        unit = match["u"].lower()
        if unit in ("g", "克") and re.match(r"\s*(?:乳?蛋白|脂肪|糖|/\s*100)", text[match.end():]):
            continue
        multiplier = MULTIPLIER.match(text[match.end():])
        if multiplier is None and unit in UNIT_SCALE:
            # A separator is often omitted in short headlines, e.g. "15ml 2瓶".
            multiplier = re.match(r"^\s*(\d+)\s*(瓶|袋|包|支|盒|桶)", text[match.end():])
        outer_count = Decimal(multiplier[1]) if multiplier else Decimal(1)
        outer_unit = multiplier[2] if multiplier and multiplier[2] else None
        remainder = text[match.end() + (multiplier.end() if multiplier else 0):]
        purchase = re.match(r"\s*(?:(?:买|拍|下单)\s*)?(\d+)\s*件", remainder)
        if purchase:
            outer_count *= Decimal(purchase[1])
            if outer_unit is None:
                outer_unit = "件"
        if multiplier:
            consumed_until = match.end() + multiplier.end()
        if unit in UNIT_SCALE:
            base, scale = UNIT_SCALE[unit]
            physical.append({"amount": number * scale * outer_count, "unit": base, "outer_count": outer_count, "outer_unit": outer_unit, "text": match.group() + (multiplier.group() if multiplier else "")})
        elif unit in ("抽", "张", "片", "粒", "条", "支", "双", "个", "只", "件", "台", "套", "盒", "瓶", "包", "袋", "桶", "杯"):
            # Sheet/piece counts are comparable; shipping packs are a fallback only.
            counted.append({"amount": number * outer_count, "unit": unit, "outer_count": outer_count, "outer_unit": outer_unit, "text": match.group() + (multiplier.group() if multiplier else "")})
    pool = physical or [item for item in counted if item["unit"] in ("抽", "张", "片", "粒", "条", "支", "双")] or counted
    if pool:
        first = pool[0]
        packs = [item for item in counted if item["unit"] in ("盒", "瓶", "袋", "包", "桶")]
        if (len(physical) == 1 and len(packs) == 1 and first["outer_count"] == 1
                and Decimal(packs[0]["amount"]) > 1 and not re.search(r"赠|送", text)):
            first = {**first, "amount": first["amount"] * packs[0]["amount"],
                     "outer_count": packs[0]["amount"], "outer_unit": packs[0]["unit"]}
        same = [item for item in pool if item["unit"] == first["unit"]]
        # Explicit plus-separated component weights are totals; repeated prose is not.
        if len(same) > 1 and "+" in text:
            first = {**first, "amount": sum((item["amount"] for item in same), Decimal(0)), "outer_count": Decimal(1), "outer_unit": None}
        return {**first, "amount": str(first["amount"]), "outer_count": str(first["outer_count"]), "defaulted": False}
    if default:
        return {"amount": "1", "unit": "件", "outer_count": "1", "outer_unit": None, "text": "默认1件", "defaulted": True}
    return None


def product_kind(text):
    if re.search(r"(?:牛奶|羊奶|高钙奶|全脂奶|脱脂奶)粉", normalized(text)):
        return "奶粉"
    if "牛奶巧克力" in normalized(text):
        return "巧克力"
    if re.search(r"(?<![a-z])mac\s*(?:mini|studio|pro|book)(?![a-z])", normalized(text), re.I):
        return "电脑"
    found = [word for word in PRODUCT_WORDS if word in normalized(text)]
    if found:
        return ALIASES.get(found[0], found[0])
    # A noun ending is a weak category clue, never a manually maintained price.
    clean = MEASURE.sub(" ", identity_title(text))
    for chunk in reversed(re.findall(r"[\u4e00-\u9fff]+", clean)):
        if len(chunk) >= 2 and chunk[-1] in "酱霜油粉糖茶皂纸袋裤裙鞋衣锅杯勺碗饼米":
            return chunk[-1]
    return None


def models(text):
    clean = MEASURE.sub(" ", identity_title(text))
    return set(re.findall(r"(?i)(?<![a-z0-9])(?:[a-z]+[- ]?\d+[a-z0-9-]*|\d{3,})(?![a-z0-9])", clean))


def grams(text):
    clean = identity_title(text).lower()
    clean = MEASURE.sub(" ", clean)
    clean = re.sub(r"[*x×]\s*\d+", " ", clean)
    result = set(re.findall(r"[a-z][a-z0-9-]{2,}", clean))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", clean):
        result.update(chunk[i:i + 2] for i in range(max(0, len(chunk) - 1)))
    return result


def similarity(deal_title, reference_title):
    target = identity_title(deal_title)
    candidate = identity_title(reference_title)
    a, b = grams(target), grams(candidate)
    overlap = len(a & b) / max(1, len(a))
    kind_a, kind_b = product_kind(target), product_kind(candidate)
    han = re.findall(r"[\u4e00-\u9fff]{2,}", target)
    brand = han[0][:2] if han else next(iter(re.findall(r"[a-zA-Z]{3,}", target)), "")
    brand_match = bool(brand and brand.lower() in candidate.lower())
    kind_match = bool(kind_a and kind_a == kind_b)
    if kind_a and kind_b and kind_a != kind_b:
        return {"score": 0, "match": "incompatible_type", "brand_match": brand_match}
    wanted_models = models(target)
    model_match = not wanted_models or wanted_models <= models(candidate)
    score = overlap + (0.18 if brand_match else 0) + (0.14 if kind_match else 0)
    if wanted_models and not model_match:
        score -= 0.25
    if brand_match and model_match and overlap >= 0.48:
        match = "same_product_or_series"
    elif kind_match and (overlap >= 0.12 or (not brand_match and len(kind_a) > 1)):
        match = "category_estimate"
    elif overlap >= 0.65 and model_match:
        match = "same_product_or_series" if brand_match else "category_estimate"
    else:
        match = "weak_match"
    return {"score": round(score, 4), "match": match, "brand_match": brand_match, "coverage": round(overlap, 4)}


def deal_facts(item):
    title = normalized(item.get("title"))
    body = normalized(item.get("content") or item.get("excerpt") or re.sub(r"<[^>]*>", " ", str(item.get("content_html") or "")))
    name = identity_title(title)
    if name.startswith("速") and re.match(r"^速[\s!！:：]+", body):
        name = name[1:].lstrip()
    size = quantity(name)
    if re.search(r"(?:水乳|护肤).{0,10}(?:套装|礼盒)", name) and size["unit"] != "套":
        purchase_sets = re.search(r"(?:拍|买|下单)\s*(\d+)\s*(?:件|套)", name) or re.search(r"(\d+)\s*套", name)
        size = quantity((purchase_sets[1] if purchase_sets else "1") + "套")
    if re.search(r"洗衣机(?![清槽专])|洗烘套装|滚筒|干衣机|冰箱|冷柜|热水器", name) or product_kind(name) in ("电脑", "手机", "平板电脑", "笔记本电脑", "水杯", "保温杯"):
        # Appliance capacity is a specification, not a consumable's package weight.
        size = quantity(re.sub(r"\d+(?:\.\d+)?\s*(?:kg|公斤|千克|g|克|ml|毫升|l|升)", " ", name, flags=re.I))
        if size["unit"] == "台":
            size = {**size, "unit": "件"}
    notes = []
    source_text = body + " " + normalized(re.sub(r"<[^>]*>", " ", str(item.get("content_html") or "")))
    conditions = [word for word in ("88VIP", "PLUS", "学生", "首单", "礼金", "凑单", "淘金币", "直播", "以旧换新", "试吃") if word.lower() in source_text.lower()]
    if re.search(r"领[^\s，。]{0,16}券|兑换[^\s，。]{0,16}券", source_text):
        conditions.append("领券")
    if re.search(r"APP.{0,8}特价", source_text, re.I):
        conditions.append("APP特价")
    for label, pattern in (
        ("消费券", r"消费[券劵卷]"),
        ("购物金充值", r"(?:充(?:值)?\s*\d+.{0,10}(?:购物金|购勿唫))|(?:(?:购物金|购勿唫).{0,10}充)"),
        ("淘金币", r"淘[金淦]币"),
        ("领券", r"领[^\s，。]{0,16}[劵卷]"),
        ("凑单", r"凑(?:消费[券劵卷]|后)"),
    ):
        if re.search(pattern, source_text) and label not in conditions:
            conditions.append(label)
    raw = item.get("price")
    if raw is None:
        found = price_values(title) or price_values(body)
        raw = found[-1] if found else None
    cash = Decimal(str(raw)) if raw is not None else None
    status = "ready" if cash is not None and cash >= 0 else "price_missing"
    # Prefer an explicitly stated cash payment over a rebate-equivalent headline.
    payment = re.search(r"(?:拍下|付|付款|实付)\s*[¥￥]?\s*(\d+(?:\.\d+)?)(?:\s*元)?(?=\s*(?:收货|[，,。]|返|反|赠|送))", body)
    equation = re.search(r"(\d+(?:\.\d+)?)\s*[-－]\s*(?:返|反)\s*\d+(?:\.\d+)?[^=]{0,12}=", body)
    if equation:
        cash = Decimal(equation[1])
        notes.append("使用返还等式中的原现金付款额")
    if payment:
        cash = Decimal(payment[1])
        notes.append("使用返券/返现前现金付款额")
    before_refund = re.search(r"(\d+(?:\.\d+)?)\s*[/／]\s*(?:返|反)\s*\d+(?:\.\d+)?", body)
    if before_refund:
        cash = Decimal(before_refund[1])
        notes.append("采用返还说明中明确的原现金金额")
    refund_net = re.search(r"(?:返|反)\s*(\d+(?:\.\d+)?)\s*(?:元)?\s*后", body)
    if refund_net and cash is not None and not payment and not equation:
        cash += Decimal(refund_net[1])
        notes.append("标题为返后价，已加回返还金额")
    if re.search(r"返|反.*红包|赠.*(?:津贴|卡)|收货.*返", body):
        notes.append("返红包、返卡另列，不自动冲减现金实付")
    per = PER_PRICE.search(body + " " + title)
    before_per = re.search(r"每\s*(提|瓶|袋|包|支|件|盒|桶|杯|粒|片|抽|张|双)\s*(?:到手|实付|单价)?\s*(\d+(?:\.\d+)?)\s*(?:元|亓|块|r)", body, re.I)
    if before_per and not per:
        per = re.match(r"(\d+(?:\.\d+)?) (\S+)", before_per[2] + " " + before_per[1])
    if per and cash is not None and abs(Decimal(per[1]) - cash) <= Decimal("0.05"):
        if size["defaulted"]:
            size = quantity("1" + per[2].lower())
            notes.append("明确每单位价格，按一" + per[2] + "记录")
        elif size["unit"] == "件" and per[2] in ("盒", "瓶", "袋", "包", "桶"):
            size = {**size, "unit": per[2]}
            notes.append("按明确的每包装单价统一购买件数的单位")
        if per[2] == size["unit"]:
            cash *= Decimal(size["amount"])
            notes.append("每" + per[2] + "价格已按明确总件数换算")
        elif per[2] == size.get("outer_unit"):
            cash *= Decimal(size["outer_count"])
            notes.append("正文为每" + per[2] + "单价，已按明确件数换算")
        elif per[2] in ("斤", "kg") and size["unit"] == "g":
            cash *= Decimal(size["amount"]) / (500 if per[2] == "斤" else 1000)
            notes.append("单位价格已换算成总现金额")
        elif Decimal(size["outer_count"]) > 1:
            status = "cash_quantity_ambiguous"
    if re.search(r"相当于|折合", body) and re.search(r"津贴|购物卡|猫超卡", body) and not payment:
        status = "cash_quantity_ambiguous"
    if re.search(r"(?:返卡|返现|返红包|返赠金)后", body) and not (payment or equation or before_refund):
        status = "cash_quantity_ambiguous"
    if re.search(r"(?:返|反|饭)\s*\d+(?:\.\d+)?\s*(?:元)?\s*(?:超市)?(?:卡|红包|津贴)", body) and not (payment or equation or before_refund):
        status = "cash_quantity_ambiguous"
    if re.search(r"定期购|周期购|订阅购", name):
        status = "cash_quantity_ambiguous"
        notes.append("周期总价与单期数量的关系待核实")
    if re.search(r"(?:每件|每单).{0,4}(?:赠|返).{0,12}(?:卡|金)|饭.{0,5}津贴", body) and not (payment or equation or before_refund):
        status = "cash_quantity_ambiguous"
    if len(MONEY.findall(title)) > 1 or ("男士" in body and "女士" in body and ("男士" in title or "女士" in title)):
        status = "multiple_products_or_variants"
    # Pack counts in a short headline can be expanded from explicit body evidence.
    body_size = quantity(body, default=False)
    if (body_size and size["unit"] in ("包", "袋", "瓶", "盒", "桶")
            and body_size["unit"] in ("g", "ml", "抽", "张", "片")
            and body_size.get("outer_unit") == size["unit"]
            and Decimal(body_size["outer_count"]) == Decimal(size["amount"])):
        size = body_size
        notes.append("按正文明确且包数一致的规格补全总量")
    body_purchase = re.search(r"(?:拍|买|下单)\s*(\d+)\s*件\s*(?:[💰¥￥]|到手|实付|共|合计|[，,:： ])*\s*(\d+(?:\.\d+)?)", body)
    if (body_purchase and cash is not None and Decimal(body_purchase[2]) == cash
            and not re.search(r"\d+\s*件", name) and Decimal(size["outer_count"]) == 1
            and body_size and body_size["unit"] == size["unit"]
            and (Decimal(body_size["amount"]) == Decimal(size["amount"])
                 or Decimal(body_size["outer_count"]) == Decimal(body_purchase[1])
                 and Decimal(body_size["amount"]) == Decimal(size["amount"]) * Decimal(body_purchase[1]))):
        size = {**size, "amount": str(Decimal(size["amount"]) * Decimal(body_purchase[1])),
                "outer_count": body_purchase[1], "outer_unit": "件"}
        notes.append("正文明确拍多件的总付款额，已换算总量")
    issued = re.search(r"(?:拍|买|下单)\s*\d+\s*发\s*(\d+)\s*(包|袋|瓶|盒|支|提)", body)
    if body_size and issued and size["unit"] == issued[2] and Decimal(size["amount"]) == Decimal(issued[1]) and body_size["unit"] in ("g", "ml", "抽", "片"):
        size = {**body_size, "amount": str(Decimal(body_size["amount"]) * Decimal(issued[1])), "outer_count": issued[1], "outer_unit": issued[2]}
        notes.append("按正文明确的单包规格和发货包数补全总量")
    if size["unit"] in ("盒", "瓶", "袋", "包", "桶"):
        unit_size = re.search(r"每\s*" + size["unit"] + r"\s*(\d+(?:\.\d+)?\s*(?:毫升|ml|千克|公斤|kg|克|g|升|l|抽|张|片))(?![a-z])", body, re.I)
        if unit_size:
            physical = quantity(unit_size[1], default=False)
            if physical:
                size = {**physical, "amount": str(Decimal(physical["amount"]) * Decimal(size["amount"])),
                        "outer_count": size["amount"], "outer_unit": size["unit"]}
                notes.append("按正文每包装的明确规格换算总量")
    available_at = None
    start = re.search(r"(?:今晚|今天|今日)?\s*(\d{1,2})\s*(?:点|:00)\s*(?:开[.· ]?抢|开始|开售)", body)
    if start and int(start[1]) < 24:
        local_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        begins = local_now.replace(hour=int(start[1]), minute=0, second=0, microsecond=0)
        available_at = begins.isoformat()
        if local_now < begins:
            status = "not_started"
    nutrients = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:g\s*)?(?:原生|乳)?蛋白", source_text, re.I)
    if status == "ready" and any(Decimal(value) == cash for value in nutrients) and not cash_evidence(source_text, cash):
        status = "cash_price_unverified"
        notes.append("API价格与正文营养指标相同，缺少独立付款金额依据")
    return {"title": name, "cash_total": str(cash) if cash is not None else None, "quantity": size, "status": status, "conditions": conditions, "notes": notes, "kind": product_kind(name), "available_at": available_at}


def cash_evidence(text, amount):
    return any(Decimal(value) == amount for value in price_values(text))


def price_values(text):
    matches = [(m.start(), m[1]) for m in MONEY.finditer(text)]
    matches += [(m.start(), m[1]) for m in PER_PRICE.finditer(text)]
    matches += [(m.start(), m[1]) for m in re.finditer(r"(?:实付|付款|到手|券后|售价|价格|仅需|拍下|付|[¥￥💰])\s*(\d+(?:\.\d+)?)(?![\d.])", text)]
    return [value for _, value in sorted(matches)]
