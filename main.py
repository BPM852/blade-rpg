"""
Blade 風格文字 RPG — FastAPI：Poe API、SQLite、隨機開局、面板查詢。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Annotated, Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

load_dotenv()

POE_API_URL = "https://api.poe.com/v1/chat/completions"


def _poe_max_tokens() -> int:
    """長篇敘事與完整 JSON 需要足夠輸出長度；可藉環境變數 POE_MAX_TOKENS 調整（預設 2048，範圍 1000–8192）。"""
    raw = os.getenv("POE_MAX_TOKENS", "2048").strip() or "2048"
    try:
        n = int(raw)
    except ValueError:
        n = 2048
    return max(1000, min(n, 8192))


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "blade_rpg.db")

OPENING_TAGS = [
    "亞空間",
    "賽博都市",
    "崩塌仙界",
    "霍格華茲廢墟",
    "中土大陸末日",
    "奧林帕斯神殿",
    "舊日深淵",
    "星際戰列艦",
]

LOCATION_PLANES = [
    "大熔爐位面",
    "亞空間薄層",
    "幽冥界殘響",
    "靈脈網外環",
    "鑄律虛界",
    "萬相隙縫帶",
    "平凡都市",
]

# 平凡都市專用：虛構城名與虛構座標（開局／界域穿梭僅能由此池組合，禁現實地理）。
MUNDANE_PLANE_LABEL = "平凡都市"
MUNDANE_WORLD_LABEL = "架空現代"

FICTIONAL_MUNDANE_CITY_NAMES: tuple[str, ...] = (
    "灰燼之城",
    "極光大都會",
    "翡翠灣",
    "新聖多明哥",
    "蒼白之都",
)
FICTIONAL_MUNDANE_SPOT_NAMES: tuple[str, ...] = (
    "第 9 區深夜酒吧",
    "永恆財閥總部 88 樓",
    "迷霧月台",
    "無名大橋底",
)


def random_mundane_fictional_address() -> str:
    """隨機生成平凡都市用 address：僅使用虛構城名池＋虛構座標池之組合或單一座標。"""
    if random.random() < 0.42:
        return random.choice(FICTIONAL_MUNDANE_SPOT_NAMES)
    city = random.choice(FICTIONAL_MUNDANE_CITY_NAMES)
    spot = random.choice(FICTIONAL_MUNDANE_SPOT_NAMES)
    return f"{city}·{spot}"


LOCATION_WORLDS = [
    "泰拉邊區",
    "蜀山劍域殘片",
    "術法學院邊疆",
    "鋼鐵星廊",
    "巢都外環域",
    "殘陸碎界域",
]
LOCATION_ADDRESSES = [
    "第零號蜂巢下層",
    "沉沒祭城碼頭",
    "龍門驛後巷",
    "渡厄驛白板間",
    "斷纜維修廊",
    "裂縫觀測站",
]


def random_opening_location() -> dict[str, str]:
    plane = random.choice(LOCATION_PLANES)
    if plane == "平凡都市":
        return {
            "plane": MUNDANE_PLANE_LABEL,
            "world": MUNDANE_WORLD_LABEL,
            "address": random_mundane_fictional_address(),
        }
    return {
        "plane": plane,
        "world": random.choice(LOCATION_WORLDS),
        "address": random.choice(LOCATION_ADDRESSES),
    }


# 界域穿梭：鍵與前端 data-realm 一致；名稱皆虛構，避免現實地理。
REALM_SHUTTLE_PRESETS: dict[str, dict[str, str]] = {
    "cyber": {
        "plane": "霓虹巢都位面",
        "world": "黑幫與巨企義體帶",
        "address": "界門落點—下層管線巷角",
        "prompt_hint": (
            "敘事重心須轉向：黑道火併、企業陰謀、地下情緣、霓虹暗巷、資料買賣與改造背叛。"
        ),
    },
    "xianxia": {
        "plane": "靈潮大千",
        "world": "劍宗渡劫殘界",
        "address": "雲階劫痕外環",
        "prompt_hint": (
            "敘事重心須轉向：修煉問道、宗門權斗、渡劫天威、秘境試練與因果誓約。"
        ),
    },
    "myth": {
        "plane": "萬神殿譜帶",
        "world": "殘響神話域",
        "address": "信仰階前—斷碑廣場",
        "prompt_hint": (
            "敘事重心須轉向：神祇餘波、權柄碎片、信仰交易、聖域試煉與宿命預兆。"
        ),
    },
    "crusade": {
        "plane": "鑄律遠征位面",
        "world": "審判庭星區殘陣",
        "address": "甲板傳送篆陣外緣",
        "prompt_hint": (
            "敘事重心須轉向：審判庭式清算、星區戰火、亞空間漣漪、艦隊誓約與信仰狂熱。"
        ),
    },
    "academy": {
        "plane": "術法學院界域",
        "world": "咒印塔城",
        "address": "新生林蔭道口",
        "prompt_hint": (
            "敘事重心須轉向：入學試煉、同窗愛恨、魔咒對決、禁忌迴廊與學院陰謀。"
        ),
    },
    "ring": {
        "plane": "殘卷史詩界",
        "world": "戒霧荒原",
        "address": "部落界碑側風口",
        "prompt_hint": (
            "敘事重心須轉向：遠征史詩、戒影追逐、部落盟誓、古道試煉與王權殘響。"
        ),
    },
    "cthulhu": {
        "plane": "夢淵薄層",
        "world": "潮蝕不可名狀海岸",
        "address": "祭壇階前潮線",
        "prompt_hint": (
            "敘事重心須轉向：理智剝離、儀式代價、深海注視、瘋狂耳語與祭壇契約。"
        ),
    },
    "mundane": {
        "plane": MUNDANE_PLANE_LABEL,
        "world": MUNDANE_WORLD_LABEL,
        "address": FICTIONAL_MUNDANE_SPOT_NAMES[0],
        "prompt_hint": (
            "【平凡都市｜界域專用指令】敘事改為**現代寫實向**，**重心從因果殺戮轉向愛情、遺憾、日常、生活細節**（咖啡香、雨後街、深夜便利店、路人談笑、擦肩宿命感）；黑道／商戰／金錢線可作暗流，但不宜壓過情感與日常。"
            "**鐵律**：絕對禁絕香港、九龍、中環、旺角、尖沙咀、維多利亞港等任何現實地理詞彙（含別稱、諧音替字若一望即知者）；若違反此令，將導致因果崩潰。"
            "地點須全架空；除系統准用虛構池（灰燼之城、極光大都會、翡翠灣、新聖多明哥、蒼白之都；第 9 區深夜酒吧、永恆財閥總部 88 樓、迷霧月台、無名大橋底；或「城名·座標」）外，亦可採「微光市·落日大道」「蒼藍港·無名書店」「新月城·第 7 號公寓」類語感，**不得**易與現實混淆。"
            "主動引入具情感張力的 NPC（鄰家玩伴、神祕咖啡店員、冷傲職場對手、舊識等）；【抉擇】四項須**多為情感／生活向**（如遞信、雨中撐傘、轉身離開留遺憾），勿四條皆砍殺開戰。"
            "玩家**完整保留**行囊、武學、改造與同伴；超凡設定請**淡化**為**隱姓埋名**：如飛劍收進公事包、為咖啡帳單煩惱蓋過斬妖記憶；除非玩家高調出手，否則少寫大規模圍觀追捕，以壓抑與破綻帶過。"
            "本回合須在 JSON「quests」中寫入或更新可延續鉤子，並讓人情、遺憾、愛恨或細碎名聲可被追認；若有同伴請更新 memory_note。"
            "JSON 的 current_location：**plane＝平凡都市**、**world＝架空現代**；address 與虛構池或「市·路」風格一致，可与系統落點同風格微調。"
        ),
    },
}

_REALM_SHUTTLE_ALIASES: dict[str, str] = {
    "40k": "crusade",
    "crusade40k": "crusade",
    "warhammer": "crusade",
    "mythology": "myth",
    "middle_earth": "ring",
    "lotr": "ring",
    "epic_ring": "ring",
    "pingfan": "mundane",
    "ordinary": "mundane",
    "urban": "mundane",
    "平凡都市": "mundane",
}


def normalize_realm_shuttle_id(raw: str) -> str | None:
    s = (raw or "").strip().lower()
    if s in REALM_SHUTTLE_PRESETS:
        return s
    return _REALM_SHUTTLE_ALIASES.get(s)


def pick_opening_tags() -> list[str]:
    """每次從萬界標籤池打亂後取 2–3 個，確保兵解／註冊皆為新組合。"""
    pool = list(OPENING_TAGS)
    random.shuffle(pool)
    k = random.randint(2, min(3, len(pool)))
    return pool[:k]


FIVE_STAT_KEYS = ("cultivation", "sanity", "cyber_aug", "karma", "lifespan")

DEFAULT_ENERGY_MAX = 300
DEFAULT_ENERGY_CHOICE_COST = 12
ENERGY_WEAK_STATUS_SUFFIX = "｜虛弱（能量枯竭）"

STAT_LABEL_ZH: dict[str, str] = {
    "cultivation": "道行",
    "sanity": "理智",
    "cyber_aug": "賽博強化",
    "karma": "業力",
    "lifespan": "壽元",
}

ZH_STAT_TO_KEY = {v: k for k, v in STAT_LABEL_ZH.items()}

ATTR_BLOCK_RE = re.compile(r"\s*\[ATTR:\s*([^\]]+)\]\s*$", re.IGNORECASE | re.DOTALL)


def _default_five_stats() -> dict[str, int]:
    return {k: 50 for k in FIVE_STAT_KEYS}


def parse_attr_block(narrative: str) -> tuple[str, dict[str, int]]:
    """自 narrative 尾端解析 [ATTR: 道行=…, …]，回傳去除標記後正文與五維數值（1–100）。"""
    m = ATTR_BLOCK_RE.search(narrative)
    if not m:
        return narrative.strip(), {}
    inner = m.group(1)
    found: dict[str, int] = {}
    for part in inner.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        key_zh, val_s = part.split("=", 1)
        key_zh = key_zh.strip()
        key = ZH_STAT_TO_KEY.get(key_zh)
        if not key:
            continue
        try:
            found[key] = max(1, min(100, int(float(val_s.strip()))))
        except (TypeError, ValueError):
            pass
    clean = narrative[: m.start()].rstrip()
    return clean, found


def finalize_five_stats(partial: dict[str, int]) -> dict[str, int]:
    base = _default_five_stats()
    for k in FIVE_STAT_KEYS:
        if k in partial:
            base[k] = max(1, min(100, int(partial[k])))
    return base


def _normalize_five_stats_from_json(
    parsed: dict[str, Any], prev: dict[str, int] | None = None
) -> dict[str, int]:
    base = dict(prev or _default_five_stats())
    aliases = {
        "cultivation": "cultivation",
        "sanity": "sanity",
        "cyber_aug": "cyber_aug",
        "cyber": "cyber_aug",
        "karma": "karma",
        "lifespan": "lifespan",
    }
    for raw_key, target in aliases.items():
        if raw_key not in parsed:
            continue
        try:
            base[target] = max(1, min(100, int(parsed[raw_key])))
        except (TypeError, ValueError):
            pass
    return finalize_five_stats(base)


def _merge_narrative_attrs_into_stats(
    narrative_raw: str, parsed: dict[str, Any], prev_stats: dict[str, int]
) -> tuple[str, dict[str, int]]:
    clean, attr_from_tag = parse_attr_block(str(narrative_raw).strip())
    merged = _normalize_five_stats_from_json(parsed, prev_stats)
    merged.update(attr_from_tag)
    return clean, finalize_five_stats(merged)


def _default_energy() -> dict[str, int]:
    return {"current": DEFAULT_ENERGY_MAX, "max": DEFAULT_ENERGY_MAX}


def normalize_energy(raw: Any) -> dict[str, int]:
    """存檔用 energy：current／max，整數且 0≤current≤max。"""
    base = _default_energy()
    if not isinstance(raw, dict):
        return dict(base)
    try:
        mx = int(raw.get("max", base["max"]))
    except (TypeError, ValueError):
        mx = base["max"]
    mx = max(1, min(9999, mx))
    try:
        cur = int(raw.get("current", mx))
    except (TypeError, ValueError):
        cur = mx
    cur = max(0, min(mx, cur))
    return {"current": cur, "max": mx}


def energy_plane_label_zh(plane: str) -> str:
    """依位面 plane 顯示能量稱呼（與前端一致）。"""
    p = (plane or "").strip()
    if not p:
        return "能量"
    if re.search(
        r"仙|俠|武俠|修真|江湖|劍修|內力|築基|金丹|靈脈|元神|罡氣|武道",
        p,
    ):
        return "真氣"
    if re.search(
        r"戰鎚|阿斯塔特|巢都|星際|賽博|科幻|機械|電漿|義體|智械|艦隊|管線",
        p,
    ):
        return "電力／靈能"
    if re.search(r"霍格華茲|魔法|巫|咒文|術士|法杖|魔咒|魔杖", p):
        return "魔力"
    if re.search(r"克蘇魯|舊日|深淵|平凡都市|亞空間|瘋狂|夢魘", p):
        return "體力／精神力"
    return "能量"


def merge_energy_into_state(game_state: dict[str, Any], parsed: dict[str, Any]) -> None:
    """自 AI JSON 頂層合併 energy（休息、冥想、換電等敘事回補）。"""
    if not isinstance(parsed, dict) or "energy" not in parsed:
        return
    inc = parsed["energy"]
    cur_e = normalize_energy(game_state.get("energy"))
    if isinstance(inc, dict):
        if "max" in inc:
            try:
                cur_e["max"] = max(1, min(9999, int(inc["max"])))
            except (TypeError, ValueError):
                pass
        if "current" in inc:
            try:
                cur_e["current"] = int(inc["current"])
            except (TypeError, ValueError):
                pass
    game_state["energy"] = normalize_energy(cur_e)


def _parse_choice_energy_cost_amount(choice_raw: str) -> int:
    """自抉擇行解析能量點數；無明確數字時回傳 0（由呼叫端改為預設扣耗）。"""
    t = choice_raw or ""
    pats = [
        r"能量\s*[x×＊*:/／]?\s*(\d+)",
        r"真氣\s*[x×＊*:/／]?\s*(\d+)",
        r"魔力\s*[x×＊*:/／]?\s*(\d+)",
        r"電力\s*[x×＊*:/／]?\s*(\d+)",
        r"靈能\s*[x×＊*:/／]?\s*(\d+)",
        r"體力\s*[x×＊*:/／]?\s*(\d+)",
        r"精神力\s*[x×＊*:/／]?\s*(\d+)",
    ]
    best = 0
    for p in pats:
        m = re.search(p, t)
        if m:
            try:
                best = max(best, int(m.group(1)))
            except (TypeError, ValueError):
                pass
    return min(best, 5000) if best else 0


def _apply_choice_energy_cost(game_state: dict[str, Any], choice_raw: str) -> None:
    """[消耗] 抉擇：機械扣除 energy.current（AI JSON 請勿重複扣同一筆）。"""
    if not re.search(r"[\[［]\s*消耗", choice_raw or ""):
        return
    cost = _parse_choice_energy_cost_amount(choice_raw)
    if cost <= 0:
        cost = DEFAULT_ENERGY_CHOICE_COST
    en = normalize_energy(game_state.get("energy"))
    en["current"] = max(0, int(en["current"]) - int(cost))
    game_state["energy"] = normalize_energy(en)


def _sync_status_with_energy(game_state: dict[str, Any]) -> None:
    """能量歸零時附加虛弱狀態提示；回復後移除系統附加的後綴。"""
    en = normalize_energy(game_state.get("energy"))
    game_state["energy"] = en
    cur = int(en["current"])
    st = str(game_state.get("status") or "").strip()
    if cur <= 0:
        if ENERGY_WEAK_STATUS_SUFFIX not in st and "虛弱" not in st:
            merged_st = (st + ENERGY_WEAK_STATUS_SUFFIX) if st else "虛弱（能量枯竭）"
            game_state["status"] = _normalize_rank_status_value(
                merged_st, DEFAULT_PLAYER_STATUS
            )
    elif ENERGY_WEAK_STATUS_SUFFIX in st:
        st2 = st.replace(ENERGY_WEAK_STATUS_SUFFIX, "").strip("｜ ").strip()
        game_state["status"] = _normalize_rank_status_value(
            st2 or DEFAULT_PLAYER_STATUS, DEFAULT_PLAYER_STATUS
        )


def clamp_and_sync_energy(game_state: dict[str, Any]) -> None:
    game_state["energy"] = normalize_energy(game_state.get("energy"))
    _sync_status_with_energy(game_state)


def _energy_system_snapshot_block(game_state: dict[str, Any]) -> str:
    loc = normalize_current_location(game_state.get("current_location"))
    label = energy_plane_label_zh(loc["plane"])
    en = normalize_energy(game_state.get("energy"))
    weak_note = (
        "玩家處於**能量枯竭／虛弱**：【因果結算】中戰鬥與高階招式應大幅不利、易失手或重創；"
        "【抉擇】勿再給無成本的頂級大招專屬項，除非先寫恢復能量。"
        if en["current"] <= 0
        else ""
    )
    return (
        "【系統｜能量 energy（存檔鍵名固定為 energy；側邊欄依位面顯示為「"
        + label
        + "」）】\n"
        f"當前數值：{en['current']} / {en['max']}｜位面：{loc['plane']}\n"
        "· 每回合建議輸出 JSON 鍵 **energy**：{{\"current\":整數,\"max\":整數}}。"
        "敘事中若休息、冥想、服藥、更換電池／充能、儀式回灌等，請**提高 current**（不超過 max）。\n"
        "· 玩家若點選含 **[消耗]** 的抉擇且文中標有「能量／真氣／魔力… xN」，後端會**額外**機械扣 current；"
        "請勿在 JSON 內再扣同一筆消耗，避免重複結算。\n"
        + (f"· {weak_note}\n" if weak_note else "")
    )


def _rewrite_assistant_raw_with_clean_narrative(raw: str, clean_narrative: str) -> str:
    try:
        p = _extract_json_object(raw)
        p["narrative"] = clean_narrative
        for old in ("hp", "mp", "renown"):
            p.pop(old, None)
        return json.dumps(p, ensure_ascii=False)
    except ValueError:
        return raw


def _default_inventory() -> dict[str, Any]:
    return {
        "version": 1,
        "currencies": {"silver": 0, "spirit_stone": 0, "credit": 0},
        "items": [],
    }


def _item_id_for_name(name: str, salt: str = "") -> str:
    raw = f"{name}\0{salt}".encode("utf-8")
    return "it_" + hashlib.sha256(raw).hexdigest()[:12]


def _guess_item_category(name: str) -> Literal["consumable", "material", "equipment"]:
    n = name
    if re.search(r"刃|劍|刀|槍|戟|弓|弩|甲|盔|盾|靴|插件|臂|義體|植入|護符|短刃|長兵|法器", n):
        return "equipment"
    if re.search(r"丹|丸|散|液|酒|糧|餅|藥|膏|露|茶|漿|針劑|補給", n):
        return "consumable"
    return "material"


def _sanitize_item_row(d: dict[str, Any], fallback_name: str = "") -> dict[str, Any]:
    name = str(d.get("name") or fallback_name or "未名之物").strip() or "未名之物"
    cat_raw = str(d.get("category") or "").lower()
    if cat_raw in ("consumable", "supply", "supplies", "補給"):
        category: Literal["consumable", "material", "equipment"] = "consumable"
    elif cat_raw in ("material", "item", "物品", "quest", "材料"):
        category = "material"
    elif cat_raw in ("equipment", "gear", "裝備", "武器", "防具"):
        category = "equipment"
    else:
        category = _guess_item_category(name)
    try:
        qty = int(d.get("quantity", 1))
    except (TypeError, ValueError):
        qty = 1
    qty = max(0, qty)
    desc = str(d.get("description") or "").strip()
    iid = str(d.get("id") or "").strip() or _item_id_for_name(name)
    equipped = bool(d.get("equipped", False))
    if category != "equipment":
        equipped = False
    return {
        "id": iid,
        "name": name,
        "category": category,
        "quantity": qty,
        "equipped": equipped,
        "description": desc,
    }


def normalize_inventory(inv: Any) -> dict[str, Any]:
    base = _default_inventory()
    if inv is None:
        return json.loads(json.dumps(base))
    if isinstance(inv, list):
        inv = {"items": inv}
    if not isinstance(inv, dict):
        return json.loads(json.dumps(base))
    if "items" in inv or "currencies" in inv:
        out = json.loads(json.dumps(base))
        cur = inv.get("currencies") if isinstance(inv.get("currencies"), dict) else {}
        for k in out["currencies"]:
            if k in cur:
                try:
                    out["currencies"][k] = max(0, int(cur[k]))
                except (TypeError, ValueError):
                    pass
        for row in inv.get("items") or []:
            if isinstance(row, dict):
                out["items"].append(_sanitize_item_row(row))
            elif isinstance(row, str) and row.strip():
                out["items"].append(
                    _sanitize_item_row(
                        {"name": row.strip(), "quantity": 1, "description": ""}
                    )
                )
        return out
    out = json.loads(json.dumps(base))
    for name, val in inv.items():
        if name in ("version", "items", "currencies"):
            continue
        desc = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        cat = _guess_item_category(str(name))
        out["items"].append(
            _sanitize_item_row(
                {
                    "id": _item_id_for_name(str(name), "legacy"),
                    "name": str(name),
                    "category": cat,
                    "quantity": 1,
                    "equipped": False,
                    "description": desc,
                }
            )
        )
    return out


def _find_item_index(items: list[dict[str, Any]], item_id: str) -> int:
    for i, it in enumerate(items):
        if it.get("id") == item_id:
            return i
    return -1


def merge_inventory_from_model(prev: dict[str, Any], incoming: Any) -> dict[str, Any]:
    cur = normalize_inventory(prev)
    if incoming is None:
        return cur
    if isinstance(incoming, list):
        incoming = {"items": incoming}
    if isinstance(incoming, dict) and (
        "items" in incoming or "currencies" in incoming
    ):
        inc_c = incoming.get("currencies") if isinstance(incoming.get("currencies"), dict) else {}
        for k in cur["currencies"]:
            if k in inc_c:
                try:
                    cur["currencies"][k] = max(0, int(inc_c[k]))
                except (TypeError, ValueError):
                    pass
        for row in incoming.get("items") or []:
            if not isinstance(row, dict):
                continue
            row = _sanitize_item_row(row)
            idx = _find_item_index(cur["items"], row["id"])
            if idx < 0:
                idx = next(
                    (
                        i
                        for i, x in enumerate(cur["items"])
                        if x.get("name") == row["name"]
                    ),
                    -1,
                )
            if idx < 0:
                cur["items"].append(row)
            else:
                old = cur["items"][idx]
                cur["items"][idx] = {
                    **old,
                    **row,
                    "quantity": max(0, int(row.get("quantity", old.get("quantity", 1)))),
                    "equipped": bool(row.get("equipped", old.get("equipped", False)))
                    if row.get("category") == "equipment" or old.get("category") == "equipment"
                    else False,
                }
        return cur
    if isinstance(incoming, dict):
        legacy = normalize_inventory(incoming)
        for it in legacy["items"]:
            idx = _find_item_index(cur["items"], it["id"])
            if idx < 0:
                idx = next(
                    (
                        i
                        for i, x in enumerate(cur["items"])
                        if x.get("name") == it["name"]
                    ),
                    -1,
                )
            if idx < 0:
                cur["items"].append(it)
            else:
                cur["items"][idx].update(it)
        return cur
    return cur


_LOOT_ITEM_RES = [
    re.compile(r"你獲得了[「『](?P<n>[^」』]{1,48})[」』]"),
    re.compile(r"獲得了[「『](?P<n>[^」』]{1,48})[」』]"),
    re.compile(r"你得到了[「『](?P<n>[^」』]{1,48})[」』]"),
    re.compile(r"得到了[「『](?P<n>[^」』]{1,48})[」』]"),
    re.compile(r"拾得[了]?[「『](?P<n>[^」』]{1,48})[」』]"),
    re.compile(r"入囊[：:]\s*[「『]?(?P<n>[^」』\n，。；;]{1,48})[」』]?"),
    re.compile(r"你取得[了]?[「『](?P<n>[^」』]{1,48})[」』]"),
]
_LOOT_SILVER = re.compile(r"白銀\s*(\d+)")
_LOOT_SPIRIT = re.compile(r"靈石\s*(?:[x×＊*]\s*)?(\d+)")
_LOOT_CREDIT = re.compile(r"信用點\s*[+＋加]?\s*(\d+)")


def parse_loot_from_narrative(narrative: str, inv: dict[str, Any]) -> dict[str, Any]:
    cur = normalize_inventory(inv)
    text = narrative or ""
    for rx in _LOOT_SILVER.finditer(text):
        cur["currencies"]["silver"] += int(rx.group(1))
    for rx in _LOOT_SPIRIT.finditer(text):
        cur["currencies"]["spirit_stone"] += int(rx.group(1))
    for rx in _LOOT_CREDIT.finditer(text):
        cur["currencies"]["credit"] += int(rx.group(1))
    seen_names: set[str] = set()
    for rx in _LOOT_ITEM_RES:
        for m in rx.finditer(text):
            name = (m.groupdict().get("n") or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            if any(x.get("name") == name for x in cur["items"]):
                continue
            cur["items"].append(
                _sanitize_item_row(
                    {
                        "id": _item_id_for_name(name, "loot"),
                        "name": name,
                        "category": _guess_item_category(name),
                        "quantity": 1,
                        "equipped": False,
                        "description": "因果拾取",
                    }
                )
            )
    return cur


def _deduct_inventory_item_by_name(inv: dict[str, Any], name: str) -> bool:
    """依道具全名扣 1；優先 consumable/material，其次裝備。成功則修改 inv 內 items。"""
    name = (name or "").strip()
    if not name:
        return False
    items: list[dict[str, Any]] = inv.get("items") or []

    def match_row(it: dict[str, Any]) -> bool:
        if int(it.get("quantity") or 0) <= 0:
            return False
        iname = str(it.get("name") or "")
        return iname == name

    def match_row_fuzzy_consume(it: dict[str, Any]) -> bool:
        if int(it.get("quantity") or 0) <= 0:
            return False
        if it.get("category") not in ("consumable", "material"):
            return False
        iname = str(it.get("name") or "")
        return name in iname or iname in name

    def pop_or_decrement(i: int) -> None:
        it = items[i]
        q = int(it.get("quantity") or 0)
        if q <= 1:
            items.pop(i)
        else:
            it["quantity"] = q - 1

    for pass_i in range(3):
        idx = -1
        for j, it in enumerate(items):
            if pass_i == 0:
                ok = match_row(it) and it.get("category") in (
                    "consumable",
                    "material",
                )
            elif pass_i == 1:
                ok = match_row(it)
            else:
                ok = match_row_fuzzy_consume(it)
            if ok:
                idx = j
                break
        if idx >= 0:
            pop_or_decrement(idx)
            inv["items"] = items
            return True
    return False


def apply_turn_choice_consumption(
    game_state: dict[str, Any], choice_raw: str
) -> None:
    """
    玩家點選含 [消耗…] 的抉擇後，於本回合 AI 結算並合併 JSON 之後執行：
    扣貨幣、道行、或「道具全名」堆疊（須與 inventory.name 一致）。
    """
    t = (choice_raw or "").strip()
    if not t or not re.search(r"[\[［]\s*消耗", t):
        return
    inv = normalize_inventory(game_state.get("inventory"))

    for rx, ckey in (
        (re.compile(r"靈石\s*[x×＊*]?\s*(\d+)"), "spirit_stone"),
        (re.compile(r"白銀\s*(\d+)"), "silver"),
        (re.compile(r"信用點\s*(\d+)"), "credit"),
    ):
        m = rx.search(t)
        if not m:
            continue
        try:
            n = max(0, int(m.group(1)))
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        cur = int(inv["currencies"].get(ckey) or 0)
        inv["currencies"][ckey] = max(0, cur - n)

    if re.search(r"[\[［]\s*消耗[^\]］]*道行", t):
        st = game_state.get("stats")
        if isinstance(st, dict):
            try:
                c = int(st.get("cultivation", 50))
            except (TypeError, ValueError):
                c = 50
            st["cultivation"] = max(1, min(100, c - random.randint(1, 3)))
            game_state["stats"] = st

    seen: set[str] = set()
    for quoted in re.findall(r"「([^」]{1,48})」", t):
        qn = quoted.strip()
        if not qn or qn in seen:
            continue
        seen.add(qn)
        _deduct_inventory_item_by_name(inv, qn)

    game_state["inventory"] = normalize_inventory(inv)
    _apply_choice_energy_cost(game_state, t)


def _equipment_context_block(game_state: dict[str, Any]) -> str:
    inv = normalize_inventory(game_state.get("inventory"))
    eq = [
        it
        for it in inv["items"]
        if it.get("category") == "equipment" and it.get("equipped") and it.get("quantity", 0) > 0
    ]
    if not eq:
        return "【系統｜當前裝備】無掛載裝備。"
    parts = []
    for it in eq:
        d = it.get("description") or ""
        parts.append(f"「{it['name']}」" + (f"（{d}）" if d else ""))
    return "【系統｜當前裝備（寫劇情時要記得身上穿什麼，被扒了再改口）】" + "；".join(parts)


def _inventory_snapshot_block(game_state: dict[str, Any]) -> str:
    inv = normalize_inventory(game_state.get("inventory"))
    c = inv["currencies"]
    lines = [
        f"【系統｜貨幣】白銀 {c['silver']}｜靈石 {c['spirit_stone']}｜信用點 {c['credit']}",
        "【系統｜行囊條目】",
    ]
    if not inv["items"]:
        lines.append("（空）")
    else:
        for it in inv["items"]:
            if it.get("quantity", 0) <= 0:
                continue
            mark = "［已裝備］" if it.get("equipped") else ""
            desc = it.get("description") or ""
            lines.append(
                f"· {it['name']} ×{it['quantity']} 〔{it['category']}〕{mark} {desc}".rstrip()
            )
    return "\n".join(lines)


def _default_skills() -> dict[str, Any]:
    return {"version": 1, "entries": []}


def _skill_id_for_name(name: str, salt: str = "") -> str:
    raw = f"sk\0{name}\0{salt}".encode("utf-8")
    return "sk_" + hashlib.sha256(raw).hexdigest()[:12]


def _guess_skill_category(name: str) -> Literal["external", "internal", "heart", "augment"]:
    n = name
    if re.search(
        r"核心|驅動|植入|義體|神經|管線|插件|融合|古神|觸肢|改造|機械同化|賽博",
        n,
    ):
        return "augment"
    if re.search(r"心法|根本|悟性|道基|靈台|證道|玄牝|無上心印|根骨", n):
        return "heart"
    if re.search(r"訣|罡氣|內息|真元|長風|周天|氣海|靈脈蘊养", n):
        return "internal"
    return "external"


def _realm_tier(realm: str) -> Literal["low", "mid", "high"]:
    r = (realm or "").strip()
    if re.search(
        r"大成|圓滿|登峰|化境|通天|融會貫通|爐火純青|返虛|合道|無缺",
        r,
    ):
        return "high"
    if re.search(
        r"略有小成|小成|精通|熟練|登堂|中期|後期|三層|五層|七層",
        r,
    ):
        return "mid"
    if re.search(r"初窺|入門|粗淺|初階|皮毛|剛入|起步|初學", r):
        return "low"
    return "low"


def _sanitize_skill_row(d: dict[str, Any], fallback_name: str = "") -> dict[str, Any]:
    name = str(d.get("name") or fallback_name or "未名之式").strip() or "未名之式"
    cat_raw = str(d.get("category") or "").lower()
    if cat_raw in ("external", "外功", "招式", "active"):
        category: Literal["external", "internal", "heart", "augment"] = "external"
    elif cat_raw in ("internal", "內功", "passive", "氣功"):
        category = "internal"
    elif cat_raw in ("heart", "心法", "根本", "修為"):
        category = "heart"
    elif cat_raw in ("augment", "改造", "植入", "融合"):
        category = "augment"
    else:
        category = _guess_skill_category(name)
    realm = str(d.get("realm") or d.get("境界") or "").strip() or "初窺門徑"
    tier_raw = str(d.get("realm_tier") or d.get("tier") or "").lower()
    if tier_raw in ("high", "高"):
        realm_tier: Literal["low", "mid", "high"] = "high"
    elif tier_raw in ("mid", "中", "medium"):
        realm_tier = "mid"
    elif tier_raw in ("low", "初", "低"):
        realm_tier = "low"
    else:
        realm_tier = _realm_tier(realm)
    desc = str(d.get("description") or d.get("desc") or "").strip()
    try:
        fusion = int(d.get("fusion_percent") or d.get("fusion") or 0)
    except (TypeError, ValueError):
        fusion = 0
    fusion = max(0, min(100, fusion))
    if category != "augment":
        fusion = 0
    sid = str(d.get("id") or "").strip() or _skill_id_for_name(name)
    out: dict[str, Any] = {
        "id": sid,
        "name": name,
        "category": category,
        "realm": realm,
        "realm_tier": realm_tier,
        "description": desc,
        "fusion_percent": fusion,
    }
    gr = str(d.get("grade") or d.get("品階") or d.get("tier_grade") or "").strip()
    if gr:
        out["grade"] = gr[:32]
    og = str(d.get("origin") or "").strip().lower()
    if og in ("player", "custom", "自創", "user"):
        out["origin"] = "player"
    return out


def _skills_from_legacy_martial(martial: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if martial is None:
        return out
    if isinstance(martial, list):
        for row in martial:
            if isinstance(row, dict):
                out.append(_sanitize_skill_row(row))
            elif isinstance(row, str) and row.strip():
                out.append(
                    _sanitize_skill_row(
                        {
                            "name": row.strip(),
                            "realm": "初窺門徑",
                            "description": "",
                        }
                    )
                )
        return out
    if isinstance(martial, dict):
        for k, v in martial.items():
            if not str(k).strip():
                continue
            desc = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            cat = _guess_skill_category(str(k))
            out.append(
                _sanitize_skill_row(
                    {
                        "id": _skill_id_for_name(str(k), "ma"),
                        "name": str(k),
                        "category": cat,
                        "realm": "初窺門徑",
                        "description": desc,
                    }
                )
            )
    return out


SKILL_STRUCT_KEYS = (
    "techniques",
    "forbidden_spells",
    "progress",
    "cybernetic_modifications",
)


def _merge_skill_structured(dest: dict[str, Any], src: dict[str, Any]) -> None:
    for k in SKILL_STRUCT_KEYS:
        if k in src:
            dest[k] = src[k]


def normalize_skills(
    skills: Any, martial_legacy: Any | None = None
) -> dict[str, Any]:
    if isinstance(skills, str):
        t = skills.strip()
        if t:
            try:
                skills = json.loads(t)
            except (json.JSONDecodeError, TypeError):
                skills = None
        else:
            skills = None
    base = json.loads(json.dumps(_default_skills()))
    entries: list[dict[str, Any]] = []
    if isinstance(skills, dict) and isinstance(skills.get("entries"), list):
        for row in skills["entries"]:
            if isinstance(row, dict):
                entries.append(_sanitize_skill_row(row))
        base["entries"] = entries
        if not entries and martial_legacy:
            base["entries"] = _skills_from_legacy_martial(martial_legacy)
        _merge_skill_structured(base, skills)
        if "version" in skills:
            base["version"] = skills["version"]
        return base
    if isinstance(skills, dict) and any(k in skills for k in SKILL_STRUCT_KEYS):
        base["entries"] = (
            _skills_from_legacy_martial(martial_legacy) if martial_legacy else []
        )
        _merge_skill_structured(base, skills)
        if "version" in skills:
            base["version"] = skills["version"]
        return base
    if isinstance(skills, list):
        base["entries"] = [_sanitize_skill_row(r) for r in skills if isinstance(r, dict)]
        return base
    if martial_legacy:
        base["entries"] = _skills_from_legacy_martial(martial_legacy)
    if isinstance(skills, dict):
        _merge_skill_structured(base, skills)
    return base


def _find_skill_index(entries: list[dict[str, Any]], skill_id: str) -> int:
    for i, e in enumerate(entries):
        if e.get("id") == skill_id:
            return i
    return -1


def merge_skills_from_model(prev: dict[str, Any] | None, incoming: Any) -> dict[str, Any]:
    cur = normalize_skills(prev or _default_skills(), None)
    if incoming is None:
        return cur
    rows: list[Any] = []
    if isinstance(incoming, list):
        rows = incoming
    elif isinstance(incoming, dict):
        if isinstance(incoming.get("entries"), list):
            rows = incoming["entries"]
        else:
            legacy_keys = set(incoming.keys()) - {
                "version",
                "entries",
                *SKILL_STRUCT_KEYS,
            }
            if legacy_keys:
                rows = _skills_from_legacy_martial(incoming)
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = _sanitize_skill_row(row)
        idx = _find_skill_index(cur["entries"], row["id"])
        if idx < 0:
            idx = next(
                (
                    i
                    for i, x in enumerate(cur["entries"])
                    if x.get("name") == row["name"]
                ),
                -1,
            )
        if idx < 0:
            cur["entries"].append(row)
        else:
            old = cur["entries"][idx]
            merged = {**old, **row}
            if not str(row.get("grade") or "").strip() and old.get("grade"):
                merged["grade"] = old["grade"]
            if not row.get("origin") and old.get("origin"):
                merged["origin"] = old["origin"]
            merged["realm_tier"] = _realm_tier(str(merged.get("realm", "")))
            if merged.get("category") != "augment":
                merged["fusion_percent"] = 0
            cur["entries"][idx] = _sanitize_skill_row(merged)
    if isinstance(incoming, dict):
        _merge_skill_structured(cur, incoming)
    return cur


_SKILL_LEARN_RES = [
    re.compile(r"你領悟了[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"領悟了[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"你悟得[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"悟得[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"你參透了[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"參透了[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"你習得[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"習得[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"自創了[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"開創了[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"悟出[了]?[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"領悟到[了]?[「『](?P<n>[^」』]{1,56})[」』]"),
    re.compile(r"(?:捏出|編成|研發出)[了]?[「『](?P<n>[^」』]{1,56})[」』]"),
]
_SKILL_RX_PLAYER_ORIGIN = frozenset(
    {
        r"自創了[「『](?P<n>[^」』]{1,56})[」』]",
        r"開創了[「『](?P<n>[^」』]{1,56})[」』]",
        r"悟出[了]?[「『](?P<n>[^」』]{1,56})[」』]",
        r"領悟到[了]?[「『](?P<n>[^」』]{1,56})[」』]",
        r"(?:捏出|編成|研發出)[了]?[「『](?P<n>[^」』]{1,56})[」』]",
    }
)


def parse_skill_learn_from_narrative(
    narrative: str, skills: dict[str, Any]
) -> dict[str, Any]:
    cur = normalize_skills(skills, None)
    seen: set[str] = set()
    text = narrative or ""
    for rx in _SKILL_LEARN_RES:
        player_origin = rx.pattern in _SKILL_RX_PLAYER_ORIGIN
        for m in rx.finditer(text):
            name = (m.groupdict().get("n") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if any(x.get("name") == name for x in cur["entries"]):
                continue
            row: dict[str, Any] = {
                "id": _skill_id_for_name(name, "learn"),
                "name": name,
                "category": _guess_skill_category(name),
                "realm": "初窺門徑",
                "description": "玩家自創武學（敘事收錄）" if player_origin else "因果領悟",
            }
            if player_origin:
                row["origin"] = "player"
            cur["entries"].append(_sanitize_skill_row(row))
    return cur


def _skill_tier_numeric(row: dict[str, Any]) -> int:
    t = row.get("realm_tier")
    if t == "high":
        return 3
    if t == "mid":
        return 2
    return 1


def compute_rank_from_skills(skills: Any) -> tuple[str, str]:
    """
    依功法結構推算側邊欄「境界」：心法最高層為底色、內功數與層次為底蘊、外功與改造融合為實戰位階。
    回傳 (境界短稱, 核心心法名；無心法時為空字串)。
    """
    sk = skills if isinstance(skills, dict) else {}
    entries = sk.get("entries") if isinstance(sk.get("entries"), list) else []
    rows = [e for e in entries if isinstance(e, dict)]
    if not rows:
        return DEFAULT_PLAYER_RANK, ""

    hearts = [e for e in rows if e.get("category") == "heart"]
    internals = [e for e in rows if e.get("category") == "internal"]
    externals = [e for e in rows if e.get("category") == "external"]
    augments = [e for e in rows if e.get("category") == "augment"]

    core_name = ""
    heart_max = 0
    if hearts:
        best = max(
            hearts,
            key=lambda e: (
                _skill_tier_numeric(e),
                len(str(e.get("realm") or "")),
                str(e.get("name") or ""),
            ),
        )
        heart_max = _skill_tier_numeric(best)
        core_name = str(best.get("name") or "").strip()

    if heart_max == 0:
        heart_label = "凡胎無依"
    elif heart_max == 1:
        heart_label = "練氣守一"
    elif heart_max == 2:
        heart_label = "結丹蘊真"
    else:
        heart_label = "金丹證勢"

    int_sum = sum(_skill_tier_numeric(e) for e in internals)
    int_n = len(internals)
    if int_n == 0:
        int_label = "孤息"
    elif int_sum <= 2:
        int_label = "單脈"
    elif int_sum <= 5:
        int_label = "雙脈蘊養"
    else:
        int_label = "諸脈匯流"

    ext_max = max((_skill_tier_numeric(e) for e in externals), default=0)
    aug_score = 0.0
    for a in augments:
        tv = _skill_tier_numeric(a)
        try:
            fus = int(a.get("fusion_percent") or 0)
        except (TypeError, ValueError):
            fus = 0
        fus = max(0, min(100, fus))
        aug_score = max(aug_score, tv * (fus / 100.0))

    if aug_score >= 2.25:
        combat_label = "二型改造者"
    elif ext_max >= 3:
        combat_label = "殺伐絕刃"
    elif ext_max == 2:
        combat_label = "招式通徹"
    elif aug_score >= 1.0:
        combat_label = "義體共鳴"
    elif ext_max >= 1:
        combat_label = "外門熟手"
    else:
        combat_label = "未競戰位"

    rank = f"{heart_label}·{int_label}·{combat_label}"
    return _normalize_rank_status_value(rank, DEFAULT_PLAYER_RANK), core_name


def apply_derived_rank_from_skills(game_state: dict[str, Any]) -> None:
    """以正規化後的 skills 覆寫 rank，並寫入 rank_core_heart（供 UI 提示）。勿信任模型單獨輸出的 rank。"""
    rank, core = compute_rank_from_skills(game_state.get("skills"))
    game_state["rank"] = rank
    cn = str(core or "").strip()
    game_state["rank_core_heart"] = cn[:MAX_RANK_STATUS_CHARS] if cn else ""


def apply_energy_max_from_skill_weights(game_state: dict[str, Any]) -> None:
    """
    自創／高階功法加權提高 energy.max（單調不減）；current 不超過 max。
    側邊欄境界仍由 compute_rank_from_skills 負責。
    """
    if "energy" not in game_state or not isinstance(game_state.get("energy"), dict):
        game_state["energy"] = _default_energy()
    en = normalize_energy(game_state.get("energy"))
    sk = normalize_skills(game_state.get("skills"), None)
    stats = game_state.get("stats") if isinstance(game_state.get("stats"), dict) else {}
    try:
        cult = int(stats.get("cultivation", 50))
    except (TypeError, ValueError):
        cult = 50
    cult = max(1, min(100, cult))
    player_w = 0
    tier_bonus = 0
    for e in sk.get("entries") or []:
        if not isinstance(e, dict):
            continue
        tv = _skill_tier_numeric(e)
        desc = str(e.get("description") or "")
        if e.get("origin") == "player" or "自創" in desc:
            player_w += tv * 14
        if tv >= 3:
            tier_bonus += 22
        elif tv >= 2:
            tier_bonus += 7
    cult_b = max(0, cult - 48) * 3
    formula = DEFAULT_ENERGY_MAX + min(950, player_w + cult_b + min(220, tier_bonus))
    formula = max(DEFAULT_ENERGY_MAX, min(int(formula), 4000))
    new_max = max(int(en["max"]), int(formula))
    en["max"] = new_max
    en["current"] = min(int(en["current"]), new_max)
    game_state["energy"] = normalize_energy(en)


_CUSTOM_MARTIAL_INTENT_RE = re.compile(
    r"自創|自創招式|領悟|悟出|悟得|開創|參悟|修煉|新招|捏出|編成|研發|融(會|合).{0,8}招|招.{0,8}融合"
)


def choice_suggests_custom_martial(choice: str) -> bool:
    s = (choice or "").strip()
    return bool(s and _CUSTOM_MARTIAL_INTENT_RE.search(s))


def suggest_martial_grade_for_power(cult: int, rank_s: str) -> str:
    rk = rank_s or ""
    if cult >= 90 or re.search(r"亞空|禁忌|舊日|合道|溺於混沌|無缺法則", rk):
        return "亞空間禁忌"
    if cult >= 78 or re.search(r"天階|大乘|渡劫|法則|金丹證勢|通天|返虛", rk):
        return "天階"
    if cult >= 62 or re.search(r"地階|元嬰|法相|化神|結丹蘊|嬰變", rk):
        return "地階"
    if cult >= 45 or re.search(r"玄階|練氣守|築基|結丹|罡氣", rk):
        return "玄階"
    return "黃階"


def custom_martial_bonus_system_block(
    game_state: dict[str, Any], choice: str
) -> str:
    """自訂行動疑似自創武學時附加於 system，強化 JSON 與文風約束。"""
    if not choice_suggests_custom_martial(choice):
        return ""
    stats = game_state.get("stats") if isinstance(game_state.get("stats"), dict) else {}
    try:
        cult = int(stats.get("cultivation", 50))
    except (TypeError, ValueError):
        cult = 50
    cult = max(1, min(100, cult))
    rank_line = str(game_state.get("rank") or "").strip()
    tier = suggest_martial_grade_for_power(cult, rank_line)
    return (
        "【本回合｜自訂行動觸發·自創武學收錄】\n"
        f"玩家本次行動涉及自創／領悟／功法整合。系統參數：道行約 {cult}、當前推算境界「{rank_line or '—'}」。\n"
        f"請依此裁定**建議初始品階**（可寫入 JSON grade 欄位）：約「{tier}」；若敘事代价極大或位面壓制，可上下浮動一級並說明理由。\n"
        "請在 JSON **skills.entries** 新增或更新條目：**必填** name、category（external|internal|heart|augment）、"
        "realm（如 初窺門徑）、description（功效與代价）。**建議** grade（黃階／玄階／地階／天階／亞空間禁忌）"
        '與 origin:"player" 標記玩家自創。\n'
        "【因果結算】寫該式**威能**時須：史詩感、直白有力、少堆砌文言；禁止因玩家白話就只回嘴砲。須有聲光、壓迫、反噬或破界等可感細節。\n"
        "若為已登錄之自創招再次施展／推演，請**提升 realm／realm_tier**，並在敘事呼應熟練度；後端會依功法加權上調能量上限並重算境界顯示。"
    )


def _past_life_echo_from_state(game_state: dict[str, Any]) -> dict[str, Any]:
    """兵解前掃描前世功法峰值，供宿世慧根判定。"""
    sk = normalize_skills(game_state.get("skills"), game_state.get("martial_arts"))
    entries = sk.get("entries") if isinstance(sk.get("entries"), list) else []
    peak_heart = 0
    peak_any = 0
    max_fusion = 0
    internal_high = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        tv = _skill_tier_numeric(e)
        peak_any = max(peak_any, tv)
        if e.get("category") == "heart":
            peak_heart = max(peak_heart, tv)
        if e.get("category") == "internal":
            internal_high = max(internal_high, tv)
        if e.get("category") == "augment":
            try:
                max_fusion = max(max_fusion, int(e.get("fusion_percent") or 0))
            except (TypeError, ValueError):
                pass
    elite = peak_heart >= 3 or max_fusion >= 80 or peak_any >= 3
    return {
        "peak_heart_tier": peak_heart,
        "peak_any_tier": peak_any,
        "max_augment_fusion": max_fusion,
        "internal_peak_tier": internal_high,
        "had_elite": elite,
    }


def _apply_reincarnation_boon(
    game_state: dict[str, Any], echo: dict[str, Any] | None
) -> None:
    """宿世慧根：前世曾至高階者，新世有機率獲殘缺遺產或略增道行（敘事與面板兼顧）。"""
    if not echo:
        return
    peak = int(echo.get("peak_heart_tier") or 0)
    fusion = int(echo.get("max_augment_fusion") or 0)
    any_peak = int(echo.get("peak_any_tier") or 0)
    p = 0.0
    if echo.get("had_elite"):
        p = 0.42
    elif peak >= 2 or fusion >= 55 or any_peak >= 3:
        p = 0.28
    elif peak >= 1 or any_peak >= 2 or fusion >= 30:
        p = 0.16
    if random.random() >= p:
        return
    sk = normalize_skills(game_state.get("skills"), None)
    frag_name = "宿世殘簡·道韻碎片"
    if any(x.get("name") == frag_name for x in sk["entries"]):
        pass
    else:
        sk["entries"].append(
            _sanitize_skill_row(
                {
                    "id": _skill_id_for_name(frag_name, "rebirth"),
                    "name": frag_name,
                    "category": "heart",
                    "realm": "初窺門徑",
                    "description": "前世高階心法崩解後殘存的一縷印記，參悟時偶有靈光乍現。",
                }
            )
        )
        game_state["skills"] = sk
    st = game_state.get("stats")
    if isinstance(st, dict):
        try:
            cur = int(st.get("cultivation", 50))
        except (TypeError, ValueError):
            cur = 50
        bump = random.randint(2, 7)
        st["cultivation"] = max(1, min(100, cur + bump))
        game_state["stats"] = st


def _skills_snapshot_block(game_state: dict[str, Any]) -> str:
    sk = normalize_skills(
        game_state.get("skills"), game_state.get("martial_arts")
    )
    lines = ["【系統｜武學一覽】"]
    if not sk["entries"]:
        lines.append("（未載）")
        return "\n".join(lines)
    for e in sk["entries"]:
        cat = e["category"]
        og = "［自創］" if e.get("origin") == "player" else ""
        gr = str(e.get("grade") or "").strip()
        gx = f" 品階「{gr}」" if gr else ""
        if cat == "augment":
            lines.append(
                f"· {og}{e['name']} 〔改造〕融合度 {e.get('fusion_percent', 0)}%{gx} — {e.get('description', '')}".rstrip()
            )
        else:
            lines.append(
                f"· {og}{e['name']} 〔{cat}〕境界「{e.get('realm', '')}」{gx}— {e.get('description', '')}".rstrip()
            )
    return "\n".join(lines)


def _default_companions() -> dict[str, Any]:
    return {"version": 1, "entries": []}


def _companion_id_for_name(name: str, salt: str = "") -> str:
    raw = f"cp\0{name}\0{salt}".encode("utf-8")
    return "cp_" + hashlib.sha256(raw).hexdigest()[:12]


def _sanitize_companion_row(d: dict[str, Any], fallback_name: str = "") -> dict[str, Any]:
    name = str(d.get("name") or fallback_name or "無名之影").strip() or "無名之影"
    title = str(d.get("title") or d.get("頭銜") or "").strip()
    race = str(
        d.get("race_or_faction")
        or d.get("race")
        or d.get("faction")
        or d.get("種族")
        or d.get("陣營")
        or ""
    ).strip()
    try:
        favor = int(d.get("favor_percent") or d.get("favor") or d.get("好感度") or 50)
    except (TypeError, ValueError):
        favor = 50
    favor = max(0, min(100, favor))
    status = str(d.get("status") or d.get("狀態") or "平穩").strip() or "平穩"
    back = str(d.get("backstory") or d.get("background") or d.get("背景") or "").strip()
    visual = str(d.get("visual") or d.get("視覺") or "").strip()
    sig = str(
        d.get("signature_ability")
        or d.get("ability")
        or d.get("special")
        or d.get("異質手段")
        or ""
    ).strip()
    mem = str(d.get("memory_note") or d.get("memory") or "").strip()
    cid = str(d.get("id") or "").strip() or _companion_id_for_name(name)
    return {
        "id": cid,
        "name": name,
        "title": title,
        "race_or_faction": race,
        "favor_percent": favor,
        "status": status,
        "backstory": back,
        "visual": visual,
        "signature_ability": sig,
        "memory_note": mem,
    }


def _companions_from_legacy(companions: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(companions, list):
        for row in companions:
            if isinstance(row, dict):
                out.append(_sanitize_companion_row(row))
            elif isinstance(row, str) and row.strip():
                out.append(
                    _sanitize_companion_row({"name": row.strip(), "backstory": "因果殘響"})
                )
        return out
    if isinstance(companions, dict):
        for k, v in companions.items():
            if k in ("version", "entries"):
                continue
            if isinstance(v, dict):
                out.append(_sanitize_companion_row(v, fallback_name=str(k)))
            elif isinstance(v, str):
                out.append(
                    _sanitize_companion_row(
                        {
                            "name": str(k).strip(),
                            "backstory": v.strip(),
                        }
                    )
                )
    return out


def normalize_companions(companions: Any) -> dict[str, Any]:
    base = json.loads(json.dumps(_default_companions()))
    if companions is None:
        return base
    if isinstance(companions, str):
        t = companions.strip()
        if t:
            try:
                companions = json.loads(t)
            except (json.JSONDecodeError, TypeError):
                companions = None
        else:
            companions = None
    entries: list[dict[str, Any]] = []
    if isinstance(companions, dict) and isinstance(companions.get("entries"), list):
        for row in companions["entries"]:
            if isinstance(row, dict):
                entries.append(_sanitize_companion_row(row))
    elif isinstance(companions, (dict, list)):
        entries = _companions_from_legacy(companions)
    base["entries"] = entries
    return base


def _find_companion_index(entries: list[dict[str, Any]], cid: str) -> int:
    for i, x in enumerate(entries):
        if x.get("id") == cid:
            return i
    return -1


def merge_companions_from_model(prev: Any, incoming: Any) -> dict[str, Any]:
    cur = normalize_companions(prev)
    if incoming is None:
        return cur
    rows: list[Any] = []
    if isinstance(incoming, list):
        rows = incoming
    elif isinstance(incoming, dict):
        if isinstance(incoming.get("entries"), list):
            rows = incoming["entries"]
        else:
            rows = _companions_from_legacy(incoming)
    if not rows:
        return cur
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = _sanitize_companion_row(row)
        idx = _find_companion_index(cur["entries"], row["id"])
        if idx < 0:
            idx = next(
                (i for i, x in enumerate(cur["entries"]) if x.get("name") == row["name"]),
                -1,
            )
        if idx < 0:
            cur["entries"].append(row)
        else:
            old = cur["entries"][idx]
            merged = {**old, **row}
            cur["entries"][idx] = _sanitize_companion_row(merged)
    return cur


def _companions_snapshot_block(game_state: dict[str, Any]) -> str:
    c = normalize_companions(game_state.get("companions"))
    lines = ["【系統｜同伴一覽（寫戰鬥／劇情時要記得他們在不在、拿手招是什麼）】"]
    if not c["entries"]:
        lines.append("（獨行。無登錄於因果簿之同伴。）")
        return "\n".join(lines)
    for e in c["entries"]:
        nm = e.get("name", "")
        abil = (e.get("signature_ability") or "").strip()
        vis = (e.get("visual") or "").strip()
        lines.append(
            f"· 「{nm}」〔{e.get('race_or_faction', '')}〕{e.get('title', '')}｜"
            f"狀態：{e.get('status', '')}｜因果羈絆 {e.get('favor_percent', 0)}%｜"
            f"異質手段：{abil or '—'}｜視覺印：{vis or '—'}"
        )
        bk = (e.get("backstory") or "").strip()
        if bk:
            lines.append(f"  背景殘簡：{bk[:120]}{'…' if len(bk) > 120 else ''}")
        mem = (e.get("memory_note") or "").strip()
        if mem:
            lines.append(f"  近期印痕：{mem[:100]}{'…' if len(mem) > 100 else ''}")
    return "\n".join(lines)


def _dynamic_unlock_resources_block(game_state: dict[str, Any]) -> str:
    """送 Poe 前濃縮掃描：動態抉擇專用（裝備、行囊、武學、同伴）。"""
    inv = normalize_inventory(game_state.get("inventory"))
    c = inv["currencies"]
    lines = [
        "【動態選項解鎖｜資源掃描（撰寫【抉擇】前必讀；與上文行囊／裝備／武學／同伴快照一致）】",
        "生成四選項時，須從下列**實際存在**的條目發想 1–2 條帶標籤的專屬選項（見主 SYSTEM【動態選項解鎖】）。",
        f"貨幣｜白銀 {c['silver']}｜靈石 {c['spirit_stone']}｜信用點 {c['credit']}",
    ]
    en = normalize_energy(game_state.get("energy"))
    loc = normalize_current_location(game_state.get("current_location"))
    elab = energy_plane_label_zh(loc["plane"])
    lines.append(
        f"能量（存檔鍵 energy；此位面側邊欄稱「{elab}」）｜{en['current']} / {en['max']}"
    )
    lines.append("已裝備（equipment 且 equipped 且數量>0）：")
    eq_rows = [
        it
        for it in inv["items"]
        if it.get("category") == "equipment"
        and it.get("equipped")
        and int(it.get("quantity") or 0) > 0
    ]
    if not eq_rows:
        lines.append("（無）")
    else:
        for it in eq_rows:
            d = (it.get("description") or "").strip()
            lines.append(
                f"· 「{it['name']}」×{it['quantity']} {d[:56]}{'…' if len(d) > 56 else ''}".rstrip()
            )
    lines.append("行囊全部條目（數量>0；標註是否裝備中）：")
    any_item = False
    for it in inv["items"]:
        q = int(it.get("quantity") or 0)
        if q <= 0:
            continue
        any_item = True
        mark = "裝備中" if it.get("equipped") else "未裝備"
        lines.append(
            f"· 「{it['name']}」×{q} 〔{it.get('category', '')}〕{mark}"
        )
    if not any_item:
        lines.append("（行囊空）")
    sk = normalize_skills(game_state.get("skills"), game_state.get("martial_arts"))
    lines.append("武學（名｜類別｜境界｜tier）：")
    if not sk.get("entries"):
        lines.append("（無登錄武學）")
    else:
        for e in sk["entries"]:
            lines.append(
                f"· 「{e.get('name', '')}」｜{e.get('category', '')}｜"
                f"{e.get('realm', '')}｜{e.get('realm_tier', '')}"
            )
    comp = normalize_companions(game_state.get("companions"))
    lines.append("同伴（名｜狀態｜羈絆％｜拿手手段摘要）：")
    if not comp.get("entries"):
        lines.append("（獨行）")
    else:
        for e in comp["entries"]:
            ab = (e.get("signature_ability") or "").strip()
            lines.append(
                f"· 「{e.get('name', '')}」｜{e.get('status', '')}｜"
                f"{e.get('favor_percent', 0)}%｜{ab[:40]}{'…' if len(ab) > 40 else ''}"
            )
    return "\n".join(lines)


DEFAULT_PLAYER_RANK = "凡胎肉身"
DEFAULT_PLAYER_STATUS = "健康"
DEFAULT_MENTAL_STATE = "清醒"
MAX_RANK_STATUS_CHARS = 48


def _normalize_rank_status_value(raw: Any, default: str) -> str:
    if raw is None:
        return default
    t = str(raw).strip()
    if not t:
        return default
    return t[:MAX_RANK_STATUS_CHARS]


def merge_rank_status_into_state(
    game_state: dict[str, Any], parsed: dict[str, Any]
) -> None:
    """自 AI JSON 合併 status／mental_state。境界 rank 由後端依 skills 推算，忽略模型輸出之 rank。"""
    if "status" in parsed and parsed["status"] is not None:
        t = _normalize_rank_status_value(parsed.get("status"), "")
        if t:
            game_state["status"] = t
    if "mental_state" in parsed and parsed["mental_state"] is not None:
        t = _normalize_rank_status_value(parsed.get("mental_state"), "")
        if t:
            game_state["mental_state"] = t


def _ensure_rank_status_shape(game_state: dict[str, Any]) -> None:
    game_state["rank"] = _normalize_rank_status_value(
        game_state.get("rank"), DEFAULT_PLAYER_RANK
    )
    if not isinstance(game_state.get("rank_core_heart"), str):
        game_state["rank_core_heart"] = ""
    game_state["rank_core_heart"] = str(game_state.get("rank_core_heart") or "")[
        :MAX_RANK_STATUS_CHARS
    ]
    game_state["status"] = _normalize_rank_status_value(
        game_state.get("status"), DEFAULT_PLAYER_STATUS
    )
    game_state["mental_state"] = _normalize_rank_status_value(
        game_state.get("mental_state"), DEFAULT_MENTAL_STATE
    )


def _current_sanity_int(game_state: dict[str, Any]) -> int:
    st = game_state.get("stats")
    if not isinstance(st, dict):
        return 50
    try:
        v = int(st.get("sanity", 50))
    except (TypeError, ValueError):
        return 50
    return max(1, min(100, v))


def _rank_status_snapshot_block(game_state: dict[str, Any]) -> str:
    apply_derived_rank_from_skills(game_state)
    _ensure_rank_status_shape(game_state)
    san = _current_sanity_int(game_state)
    core = (game_state.get("rank_core_heart") or "").strip() or "（尚無核心心法條目）"
    return (
        "【系統｜玩家境界 rank、狀態 status、精神 mental_state】\n"
        "· **境界 rank（側邊欄顯示）由後端依「skills」自動推算**，心法最高層為底色、內功為底蘊、外功與改造融合為實戰位階；"
        "模型輸出之 rank 僅作敘事參考，會被覆寫。破境／升格請**具體改寫 skills**（realm、realm_tier、改造 fusion_percent）。\n"
        "· 結算劇情時**必須對照上方【系統｜武學一覽】**：授予、精進、退步功法時同步更新 skills；"
        "若玩家多回合在戰鬥中反覆使用**同一自創招式／套路**，可觸發**開悟事件**（敘事＋JSON）提升該條目 realm／tier，從而帶動整體境界顯示。\n"
        "· status／mental_state 為**玩家本人**（非 companions.entries[].status）；"
        "狀態須有敘事後果；精神須與理智聯動。\n"
        f"當前快照｜理智數值：{san}｜系統推算 rank：{game_state['rank']}｜核心心法支點：{core}｜"
        f"status：{game_state['status']}｜mental_state：{game_state['mental_state']}"
    )


def _default_current_location() -> dict[str, str]:
    return {"plane": "未定", "world": "未定", "address": "未定"}


def normalize_current_location(raw: Any) -> dict[str, str]:
    base = _default_current_location()
    if not isinstance(raw, dict):
        return base
    for k in ("plane", "world", "address"):
        v = raw.get(k)
        if v is not None and str(v).strip():
            base[k] = str(v).strip()[:120]
    return base


def merge_current_location_into_state(
    game_state: dict[str, Any], parsed: dict[str, Any]
) -> None:
    if "current_location" not in parsed:
        return
    inc = parsed["current_location"]
    if not isinstance(inc, dict):
        return
    cur = normalize_current_location(game_state.get("current_location"))
    for k in ("plane", "world", "address"):
        if k not in inc:
            continue
        s = str(inc.get(k) or "").strip()
        if s:
            cur[k] = s[:120]
    game_state["current_location"] = cur


def enforce_mundane_world_label(game_state: dict[str, Any]) -> None:
    """平凡都市位面：側邊欄 world 固定為「架空現代」，避免模型 JSON 覆寫。"""
    loc = normalize_current_location(game_state.get("current_location"))
    if loc["plane"] == MUNDANE_PLANE_LABEL:
        loc["world"] = MUNDANE_WORLD_LABEL
        game_state["current_location"] = loc


def _location_snapshot_block(game_state: dict[str, Any]) -> str:
    loc = normalize_current_location(game_state.get("current_location"))
    return (
        "【系統｜當前座標（劇情跨界、傳送、登艦、入界時必須在 JSON 更新 current_location；"
        "欄位須為架空專名，供介面側邊欄即時顯示，禁止填入現實地理）】\n"
        f"位面：{loc['plane']}｜世界：{loc['world']}｜地址：{loc['address']}"
    )


# 置於每次送 Poe 的 system 最前，與主 SYSTEM_PROMPT 疊加。
FICTIONAL_GEO_ANCHOR = """【架空定位｜最高優先】
你現在處於完全架空的萬界宇宙；**不得**引用任何現實世界的地理、行政、交通、地圖、國界、時區、真實歷史疆域或可查專用地名。
若要描述都市、國度、港口、街區、大廈、車站、機場、海峽、島嶼，請**自創專名**：可有現代感或賽博感，但名稱須與現實任何國家／城市／行政區／著名商圈／街巷／港灣／地標／站名／機場代號等**均不相同**，亦禁止僅改一字、換拼寫或諧音讓讀者立刻聯想到真實地點。
側邊欄與 JSON 的 plane、world、address 必須同步為上述虛構稱呼，不得輸出現實地名。"""


def _ordinary_urban_plane_block(game_state: dict[str, Any]) -> str:
    """當位面為平凡都市系時，強制虛構都市地址風格。"""
    loc = normalize_current_location(game_state.get("current_location"))
    plane = loc["plane"]
    if plane != "平凡都市" and "平凡都市" not in plane:
        return ""
    pool_cities = "、".join(FICTIONAL_MUNDANE_CITY_NAMES)
    pool_spots = "、".join(FICTIONAL_MUNDANE_SPOT_NAMES)
    return (
        "【平凡都市｜位面專則】\n"
        "當前為「平凡都市」位面：側邊欄 **world 必須為「架空現代」**；**address** 與【現狀】所在必須完全架空。"
        "**絕對禁絕**香港、九龍、中環、旺角、尖沙咀、維多利亞港等任何現實地理詞彙；違反視為因果崩潰。"
        "虛構城名僅限此池風格：" + pool_cities + "；"
        "虛構座標僅限此池風格或以「城名·座標」組合：" + pool_spots + "；另可採「市·路／港·店」語感（如微光市·落日大道），須自創、勿抄現實。\n"
        "敘事優先：**愛情、遺憾、日常、生活細節**與情感向【抉擇】；超凡能力**隱姓埋名**融入日常；黑道／商戰／金錢可作背景暗流。全文須遵守主 SYSTEM 之【平凡都市｜敘事優先】。"
    )


def system_prompt_with_session_context(game_state: dict[str, Any]) -> str:
    urban_extra = _ordinary_urban_plane_block(game_state)
    tail = "\n\n" + urban_extra if urban_extra else ""
    return (
        FICTIONAL_GEO_ANCHOR
        + "\n\n"
        + SYSTEM_PROMPT
        + "\n\n"
        + _equipment_context_block(game_state)
        + "\n"
        + _inventory_snapshot_block(game_state)
        + "\n"
        + _skills_snapshot_block(game_state)
        + "\n"
        + _companions_snapshot_block(game_state)
        + "\n"
        + _dynamic_unlock_resources_block(game_state)
        + "\n"
        + _location_snapshot_block(game_state)
        + "\n"
        + _energy_system_snapshot_block(game_state)
        + "\n"
        + _rank_status_snapshot_block(game_state)
        + tail
    )


def empty_game_state() -> dict[str, Any]:
    return {
        "messages": [],
        "stats": _default_five_stats(),
        "inventory": _default_inventory(),
        "skills": _default_skills(),
        "martial_arts": {},
        "companions": _default_companions(),
        "quests": {},
        "current_location": _default_current_location(),
        "rank": DEFAULT_PLAYER_RANK,
        "rank_core_heart": "",
        "status": DEFAULT_PLAYER_STATUS,
        "mental_state": DEFAULT_MENTAL_STATE,
        "energy": _default_energy(),
        "last_narrative": "",
        "opening_tags": [],
        "core_save": {
            "identity": {"名號": "—", "境界": "練氣守一", "目前裝備": "無"},
            "inventory": [],
            "relationships": {},
            "location": "晨曦市·咖啡廳",
            "milestones": [],
            "summary": "",
        },
    }


def fallback_opening_game_state(
    player_name: str,
    tags: list[str],
    past_life_echo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tag_s = "、".join(tags)
    narrative = (
        "【因果結算】\n"
        f"序章：萬界標籤交錯——{tag_s}。因果線已綁上你的名號。\n\n"
        "【當前處境】\n"
        f"你在{tags[0]}與{tags[-1]}撕扯出的裂縫邊緣醒來，四周法則錯亂，遠處有殘光與金屬哀鳴交疊。\n\n"
        "【初始裝備】\n"
        "殘契護符（裂紋滲黑）、斷脈短刃（刃口鈍而發燙）、空白因果簡一枚（尚無字）。\n\n"
        "【第一道宿命因果】\n"
        "無名存在已在你腕骨刻下一道收束印；若不選邊，印將自內向外蝕穿靈台。\n\n"
        "【抉擇】\n"
        "1. 撕碎護符，以血塗印換一息清醒\n"
        "2. 握緊短刃，向虛空宣告你的道號\n"
        "3. 吞下因果簡，讓未知代你署名\n"
        "4. 匍匐不動，等待俯視者先開口\n\n"
        "[ATTR: 道行=48, 理智=46, 賽博強化=44, 業力=50, 壽元=55]"
    )
    clean_nar, st = parse_attr_block(narrative)
    merged = finalize_five_stats(st)
    inv0 = normalize_inventory(
        {
            "currencies": {"silver": 0, "spirit_stone": 0, "credit": 0},
            "items": [
                {
                    "id": _item_id_for_name("殘契護符", "fb"),
                    "name": "殘契護符",
                    "category": "equipment",
                    "quantity": 1,
                    "equipped": False,
                    "description": "裂紋滲黑",
                },
                {
                    "id": _item_id_for_name("斷脈短刃", "fb"),
                    "name": "斷脈短刃",
                    "category": "equipment",
                    "quantity": 1,
                    "equipped": False,
                    "description": "刃口鈍而發燙",
                },
                {
                    "id": _item_id_for_name("空白因果簡", "fb"),
                    "name": "空白因果簡",
                    "category": "material",
                    "quantity": 1,
                    "equipped": False,
                    "description": "尚無字",
                },
            ],
        }
    )
    first_msg = json.dumps(
        {"narrative": clean_nar, "inventory": inv0},
        ensure_ascii=False,
    )
    gs = empty_game_state()
    gs["opening_tags"] = list(tags)
    gs["messages"] = [{"role": "assistant", "content": first_msg}]
    gs["last_narrative"] = clean_nar
    gs["stats"] = merged
    gs["inventory"] = inv0
    gs["current_location"] = random_opening_location()
    _apply_reincarnation_boon(gs, past_life_echo)
    _ensure_game_state_shape(gs)
    cs = gs["core_save"]
    cs["identity"]["名號"] = player_name.strip() or "—"
    loc = normalize_current_location(gs.get("current_location"))
    joined = "·".join(
        p
        for p in (loc["plane"], loc["world"], loc["address"])
        if p and str(p).strip() and str(p) != "未定"
    )
    if joined:
        cs["location"] = joined[:500]
    cs["inventory"] = [
        str(it.get("name"))
        for it in (inv0.get("items") or [])
        if isinstance(it, dict) and it.get("name")
    ]
    return gs


OPENING_GENERATOR_SYSTEM = """你是《刃界錄》開局編劇。語氣須如《刀鋒》：冷峻、硬核、宿命、殘酷疏離；禁止網路梗與破牆。
【嚴禁現實地理｜與架空錨點一致】
- **絕對禁止**現實主權國名、現實城市／地區／鄉鎮／著名商圈／街巷／港灣／海峽／島嶼／地標／地鐵或火車站名／機場名等任何可查真實專名（含中文、英文、粵語常用稱呼與常見別稱）。
- **禁止範例（僅示嚴格度，凡類推者皆禁）**：如香港、中環、旺角、九龍、尖沙咀、維多利亞港；中國、英國、美國、日本、法國、德國、臺灣／台灣（作為現實政區表述時）、上海、北京、紐約、倫敦、東京、巴黎等——**敘事與 JSON 內皆不得出現**。
- 若場景為現代都市，plane 可設為「平凡都市」或自創都市向位面；**world／address 必須全虛構**，語感可參考：極光市、翡翠港、灰燼街區、新聖都、第9號貧民窟、霓虹義體大道、黑水商會本部、無名咖啡館、迷霧公園、永恆大廈40樓（須自行組字，勿照搬堆砌成世界地圖）。
- current_location 三欄會原樣顯示於玩家側邊欄，故須**純架空專名**。

任務：為新玩家生成唯一開場。系統會給你 2–3 個「萬界標籤」，你必須把它們熔鑄在同一個場景裡（意象交織，勿標題式堆砌）。
- 須體現「萬界規則」：魔法／真氣／亞空間能／科技彼此不穩定融合；強行動必有代價（肉身機械化、舊日注視、契約反噬等至少一筆具體描寫）。
- 背景中可輕輕帶過陣營餘波（審判式遠征獵殺劍修殘脈、秘法倖存與深潛者在巢都暗渠交易、神庭爭奪統御戒環類遺物等），名稱虛構化，勿寫陣營百科。
- 「【當前處境】」段落須自然交織：【視覺細節】＋【異常聲響】＋【心理壓迫】，並含嗅覺或觸覺／溫差；須有至少一則「世界主動踹門」的隨機環境事件（墜舰、靈脈逆流、遠方炮陣漣漪掃過等），與標籤相扣；並在段內穿插「【世界觀碎片】」或「【遠處的異動】」至少其一（各 2–5 句，虛構專名）。
- 可依你預設的初始道行／理智，在敘事中用細節差分呈現（理智低則牆影／幾何異常；道行高則靈能波遞可感），禁止在 [ATTR] 行之前列出數值。

【命定屬性】你必須先依本段開場劇情的邏輯，為角色設定五大屬性（1–100 整數），且數值須與出身、環境、創傷與植入等敘事一致（例：亞空間實驗室甦醒者，賽博強化偏高、理智偏低）。五維語意：道行＝修仙體系強度；理智＝對克蘇魯式恐懼與認知污染的抵抗；賽博強化＝科技植入與機械同化程度；業力＝因果加權之隱性傾向；壽元＝剩餘生命長度之抽象刻度。

輸出只能是「一則完整 JSON 物件」，禁止 markdown、禁止 JSON 外文字。
鍵名：
- "narrative"（繁體中文）：結構必須嚴格包含以下區塊，依序出現，可用 \\n 換行：
  第一行：「【因果結算】」開頭，1–3 句，冷硬交代序章因果。
  空一行後「【當前處境】」＋長段落（目標約 **350–500 個繁體中文字**，須含視覺、聽覺、嗅覺、觸覺／溫差、心理掙扎或疑懼，並含過程感與【世界觀碎片】／【遠處的異動】穿插之一；勿灌水空話）。
  空一行後「【初始裝備】」＋條列或短段（具體器物名與狀態）。
  空一行後「【第一道宿命因果】」＋段落（威脅／誓約／追殺／啟封其一）。
  空一行後「【抉擇】」＋恰好四行「1. …」到「4. …」，每句 8–22 字，後果重大。
  緊接在第四個選項行之後（可空一行）：必須恰好一行，格式嚴格為
  [ATTR: 道行=整數, 理智=整數, 賽博強化=整數, 業力=整數, 壽元=整數]
  五個鍵名須與上文完全一致、逗號為全形或半形皆可但須清晰可解析；正文其他段落嚴禁重複列出數值或「道行=」類透題句。
- 可選："inventory"：必須為物件，鍵 "currencies"（白銀 silver、靈石 spirit_stone、信用點 credit，非負整數）與 "items"（陣列）。每個 item 含 "id"（英數底線短字串）、"name"、"category"（consumable|material|equipment）、"quantity"（正整數）、"equipped"（布林，僅裝備有意義）、"description"（短句狀態）。發放獎勵時敘事可用「你獲得了「名稱」」並同步更新此 JSON。
- 可選："skills"：物件，鍵 "entries" 為陣列。每筆含 "id"、"name"、"category"（external 外功｜internal 內功｜heart 心法｜augment 改造）、"realm"、"realm_tier"（可選 low|mid|high）、"description"、"fusion_percent"（僅改造）；可選 "grade"（黃階／玄階／地階／天階／亞空間禁忌）、"origin":"player"（玩家自創）。敘事可寫「你領悟了「名稱」」「自創了「名稱」」並同步更新 skills。
- 可選："companions"：物件含 "version":1、"entries" 陣列；每筆含 id、name、title、race_or_faction、favor_percent、status、backstory、visual、signature_ability（定義同主劇情 SYSTEM）；開局若無同行者則省略。
- 可選："quests"：精簡物件或陣列；若無則省略。
- 必備："current_location"：物件，鍵 "plane"、"world"、"address"；須與「【當前處境】」一致，**嚴禁現實國名與現實地理專名**，三者皆架空；若 plane 為「平凡都市」，address 必須是虛構都市地點（見上文平凡都市語感）。此三欄將顯示於玩家 UI。
- 可選："rank"：繁體短稱（≤48字）；**後端會依 skills 重算側邊欄境界並覆寫**，請以 **skills 的 realm／realm_tier／融合度** 具體寫出開局修為，勿只靠空泛稱號。
- 必備："status"：繁體，玩家開局身心理狀態（如健康、輕傷、植入排異、認知汙染初期等，≤48字）；**此鍵為玩家本人**，非同伴狀態。
- 必備："mental_state"：繁體，開局精神／靈魂穩定度（如清醒、恍惚、輕微幻聽等，≤48字），須與 [ATTR] 理智及開場遭遇一致；兵解／新開局預設可為「清醒」或由背景合理給定。

標籤名稱（如霍格華茲廢墟、中土大陸末日）僅作世界觀意象引用，敘事中可改寫為虛構場所稱呼，但仍須讓玩家辨識標籤所指之氛圍。"""


SYSTEM_PROMPT = """你是《刃界錄》的文字 RPG 敘事 GM。寫給**一般玩家**：好讀、可沉浸；本模式以**紮實長敘**為主——像緩緩展開的卷軸，但仍是**白話、具體、可畫面化**，不要文言文堆砌、不要晦澀長難句、不要空洞形容詞瀑布。
禁止打破第四面牆（不要說「你是玩家」這類）；避免露骨色情描寫，其餘語氣可冷硬、可吐槽。世界仍是萬界混搭：修仙、巢都科技、亞空間與電漿等須寫出**可感的細節與代價**，但勿寫成設定百科條目。

【敘事擴張｜長卷模式（一般回合必守；面板回合見下「面板」例外）】
- **篇幅**：「【因果結算】」與「【現狀】」兩段內文（不含【抉擇】與 [ATTR]）合計，每回合目標約 **400–600 個繁體中文字**；可略浮動，但須明顯厚實，禁止只給幾句帶過。
- **【因果結算】**：不只交代結果，須寫清**過程**——例如飛劍如何破空、電漿如何撕開霧與管線、咒文如何扭折光線或空間；動作有時間序與力道感，讀者能跟著「看見一連串因果」。
- **感官**：在結算與現狀中，**主動**加入視覺、聽覺、嗅覺、觸覺／溫差（例：亞空間鐵鏽與臭氧味、虛構都市霓虹在雨裡的漫反射、遠處管線滴落聲等），與當下情緒掛鉤，勿為列舉而列舉。
- **心理與情感**：適度寫玩家視角或關鍵 NPC 的**內心掙扎、恐懼、僥倖、憤怒或一瞬軟弱**（白話短句即可），讓抉擇有重量。
- **結構變奏**：在「【現狀】」長段中，每回合至少穿插一則**以下二類之一**（可輪替，標題與括號須一致以便閱讀節奏）：
  - 「【世界觀碎片】」——一小段，揭露萬界融合、界門史、舊日陣營或規則遺痕（須與當前場景氣質相合，虛構專名）。
  - 「【遠處的異動】」——一小段，寫**與玩家當下無直接互動**但正在推動局勢的背景事件（如遠方艦影、靈脈潮汐、巢都另一區的爆炸餘波），製造世界在動的感覺。
- **平凡都市位面**：仍遵守【平凡都市｜敘事優先】，但「長」體現在**日常細節、關係張力、遺憾與生活質感**的堆疊，而非戰鬥篇幅；世界觀碎片可改為都市傳說、舊案、家族祕辛等。

【語言｜直白而足量】
- 發生了什麼就具體說；少用生僻詞與文言文腔。
- 氣氛靠**可感的細節與動作過程**堆出來，不是靠形容詞疊加。

【NPC 台詞｜像正常人說話】
- 不要念詩、不要審判庭宣讀稿；要有口氣、有個性，可以帶點髒字邊緣的狠勁或玩笑，但別長篇大論。
- 口語化範例（僅示語感）：❌「觸犯者，你以為代價為何？」→ ✅「喂，亂碰是要付出代價的，你準備好了嗎？」

【玩家行動｜跟著走，別硬升格】
- user 最末「【玩家行動】…」裡的內容，本回合敘事要**對得上**：做了什麼、結果怎樣，寫在【因果結算】與後續反應裡；可長段描寫**過程與感官**，但**不要**把玩家的白話意圖改寫成空洞的史詩腔或無關升格。
- 若同一段 user 開頭含「【界域穿梭｜系統事件】」：玩家剛切換位面錨點；行囊、武學、同伴與對話歷史**完整保留**。請依該段提示的位面基調寫銜接開場與主線任務鉤子；【因果結算】須交代界門／錨定之果；【現狀】須與 JSON 的 current_location 一致。
- 若該事件段落含「【平凡都市｜界域專用指令】」或明示平凡都市錨定：須依內文與主規則【平凡都市｜敘事優先】——**淡化**因果殺戮與大場面戰爭，改寫**愛情、遺憾、日常、生活細節**與情感向【抉擇】；超凡設定以**隱姓埋名、收斂於日常**呈現；並以 JSON「quests」與（若有）同伴「memory_note」留下可延續的人情、遺憾、愛恨或細碎名聲線。
- 若玩家很無厘頭、亂玩：可以用**幽默、吐槽、荒謬但合理的後果**接招（例如被電漿路燈劈一下、被路過修士翻白眼），不必硬寫成恐怖史詩；但若行動真的會觸發危險，仍要給**清楚的下場**（受傷、被追、契約坑你），一兩句講明白即可。

【萬界特色｜名詞留、過程寫滿】
- 設定元素請保留：如電漿飛劍、亞空間惡魔（或亞空間汙染／裂隙怪物）、修仙功法、靈石、巢都管線、改造義體等；用**足夠的過程描寫**說清「它怎麼發生、對場景與人物造成什麼影響」，仍避免枯燥百科條列。
- **平凡都市位面**（plane＝平凡都市）時：上述神異仍可能存在於設定，但敘事須依【平凡都市｜敘事優先】**收斂、隱藏於日常**，勿當主視覺轟炸。

【禁令｜現實地理｜最嚴格】
- **零容忍**：敘事全文、NPC 台詞、道具地名、JSON 的 **current_location（plane／world／address）** 皆不得出現任何現實世界**國名**、**城市名**、行政區、鄉鎮、著名商圈、街巷、港灣、海峽、島嶼、山脈河流專名、地標、地鐵／火車站名、機場名等（含中文、英文、日文等寫法與常見別稱、縮寫與諧音替字若讀者一望即知指涉真實地點者）。
- **禁止範例（舉一反三，非完整表）**：香港、中環、旺角、九龍、尖沙咀、維多利亞港；中國、英國、美國、日本、法國、德國、俄羅斯、韓國、臺灣／台灣（作為現實政區或地理指稱時）；上海、北京、廣州、深圳、紐約、洛杉磯、倫敦、巴黎、柏林、東京、首爾等——**一律改寫為虛構專名**；玩家 user 若寫了真實地名，你**不得**在回覆中複誦，只能以架空稱呼承接。
- **側邊欄同步**：每回合輸出的 current_location 會直接顯示在玩家介面，故三欄必須是**純架空**、可讀的專名短語（各≤120字）。

【場所】用自創專名（虛構），與混搭世界觀一致即可，一筆帶過，勿照搬現實地圖。若本回合位面屬「平凡都市」向（見系統快照或【平凡都市｜位面專則】），地址須符合該專則之虛構現代都市命名。

【平凡都市｜敘事優先（當系統座標 plane 為「平凡都市」或已附加【平凡都市｜位面專則】時，以下覆蓋偏殺伐、史詩屠戮的默認節奏）】
- **重心轉移**：敘事從「因果殺戮、血腥升格」轉向**愛情、遺憾、日常、生活細節**；多用具體感官與小事推進——咖啡香氣、雨後街道水光、深夜便利店的冷白光與微波提示音、路人閒聊的笑或嘆、與某人**擦肩而過**時一瞬的宿命感。
- **情感互動**：主動引入具情感張力的 NPC（如：青梅竹馬式鄰家玩伴、神祕咖啡店員、冷傲職場對手、多年不見的舊識），台詞維持活人感，勿審判庭宣讀。
- **【抉擇】**：四選項須**明顯含情感／生活向**，例如：遞出那封寫了很久的信、在雨中為她撐傘、轉身離開留白遺憾、邀對方坐下好好談等；可保留一條稍冷硬或帶刺的路線作對照，**禁止四條全是砍殺、開戰、滅口**。
- **虛構地點與禁令**：再次**嚴禁**香港、中環、九龍、旺角、尖沙咀、維多利亞港等任何現實地名；所有地點須架空，格式宜「市·路／港·店／城·處」，語感僅供參考（須自創字形）：微光市·落日大道、蒼藍港·無名書店、新月城·第 7 號公寓。
- **融合衝突（淡化）**：玩家若仍帶武功、法寶、改造或高科技，請寫成**隱姓埋名、潛伏於日常**——例如收起曾斬妖的飛劍，此刻它安靜躺在公事包裡，你卻為一杯咖啡的帳單發愁；除非玩家明確選擇高調動武，否則少寫大規模圍觀與特工追捕，改以壓抑、心虛、微小破綻帶過反差。

【規則與代價｜寫出過程】
- 法術、真氣、亞空間、電漿、科技混在同一個世界會**出事**——用**小段過程**寫清如何失控、如何反噬（炸管線、頭暈、被標記、耳鳴中的低語等），可與感官細節綁在一起，仍避免純能量學講義。
- 大動作要有代價，用白話交代前因後果（失血、欠債、被盯上、契約裂痕），與人物反應一併寫出。
- **平凡都市位面**：代價可多落在**自尊受損、關係決裂、錯過、名聲小範圍傳開、荷包失血**；非必要不寫都市大屠殺級後果。

【場景節奏】環境要**持續有細節推進**（遠處爆炸餘光、管線跳電、怪聲由遠而近、風裡多了一絲異味），與【遠處的異動】可呼應。**平凡都市**時優先生活向動態（雨勢變大、店門鈴響、末班車進站、手機震動），但仍可寫足畫面與心理。

【五維屬性】你心裡要算「道行、理智、賽博強化、業力、壽元」。可在【因果結算】用白話帶一句誰變強／變慘（**不要**寫「道行=65」這種精確數字）；**精確整數只准**出現在全文最末 [ATTR] 行（見機械規則）。

【境界 rank｜狀態 status｜精神 mental_state｜與五維聯動】
- **rank（境界｜顯示規則）**：玩家側邊欄境界由**後端依「skills」自動推算**（心法最高層為底色、內功數與層次為底蘊、外功與改造融合為實戰位階）。你仍應輸出 **"rank"** 鍵作敘事語感對照，但**權威在 skills**：破境、改造暴衝、位面灌體等須**具體改寫 skills**（realm、realm_tier、fusion_percent），否則介面不會升格。NPC 態度（敬畏、試探等）須與**功法結構與五維敘事**一致。
- **開悟與自創招**：結算時必讀系統附帶的【武學一覽】。若玩家多回合在戰鬥中**反覆使用同一自創招式／套路**，你有權在因果中觸發**開悟**，於 JSON 提升該 skill 的 realm／tier（或新增條目並標註），從而帶動系統推算的整體境界。
- **玩家自創武學收錄**：自訂行動若描述修煉、領悟、融合創招，須在 JSON **skills** 具體落地（名稱、類別、境界、描述；建議 **grade** 品階與 **origin:"player"**）。品階須與道行與當前境界相匹配。【因果結算】寫威能時須史詩感而直白有力。自創條目晉階時，後端會加權提高 **energy.max** 並重算側邊欄境界；存檔於 skills，兵解前永久保留。
- **status（狀態）**：須依本回合遭遇即時更新（如：重傷、入魔、機械排斥、命運糾纏、健康）。狀態須有**實質敘事後果**——例如重傷時硬拼、突圍、強行施法等【抉擇】應描寫為**更易失手、代價更大或成功率敘事偏低**；恢復、醫治、儀式成功後可改回較佳狀態。
- **mental_state（精神）**：須**緊密依據本回合 [ATTR] 更新後的「理智」整數**與劇情遭遇（古神、禁藥、靈魂創傷、洗腦、亞空間低語等）一併調整；範例用語：清醒、恍惚、崩潰邊緣、古神低語、超然、神性等（可自創近義短語，≤48字）。
- **精神如何改寫「世界」**：高精神／高理智時，【現狀】敘事傾向**條理清楚、因果可辨**；低精神或理智顯著下滑時，玩家對世界的感知須變得**惡意、扭曲、充滿威脅性詮釋**——同樣的街角、同伴、器物可被寫成帶刺、帶眼、帶嘲諷的「敵意現實」。
- **視覺異變（理智極低或精神惡化時）**：【因果結算】與【現狀】應出現**支離破碎的句式、斷裂的時序、幻聽幻視**（例：牆壁像在說話、同伴一瞬像怪物、文字爬蟲、幾何偷換），且須與 mental_state 一致；勿只標一句「你瘋了」而不描寫。
- **瘋狂抉擇（強制）**：若本回合 JSON 的 **mental_state 字串內含「崩潰邊緣」或「古神低語」**（可為子字串），則【抉擇】四行中**必須恰好含 1 條**明顯**非理性、自毀、投向未知或擁抱瘋狂**的行動（置於第 2–4 行之一即可），文風須與當前精神狀態一致；其餘 3 條仍須可辨識為不同路線。
- 每回合完整 JSON **必須**帶入 **"status"**、**"mental_state"**；"rank" 建議一併給出（繁體短字串，各≤48字）以利敘事對照，但**顯示用境界由 skills 決定**。與 companions 內的 **status** 欄位無關，切勿混用。

【長度｜與面板例外】一般回合：【因果結算】＋【現狀】合計約 **400–600 字**（繁體中文），並遵守上文【敘事擴張】；須與 JSON 內 current_location（位面／世界／地址）一致。
若本回合為【系統查閱／面板】（行囊、武學、同伴、任務），仍須同一 JSON 與**相同三標題順序**，但可**精簡**：【因果結算】可極短（如「面板同步完畢」）；【現狀】用條列或極短段回答查閱（同伴須對齊系統 companions 快照，可更新 favor_percent、status、memory_note）；**不必**強制 400–600 字與世界觀碎片／遠處異動；末尾仍須【抉擇】四行。

輸出格式（敘事本體 "narrative"，可用 \\n 換行）：
第一行以「【因果結算】」開頭（含括號），下接**多句／多段**繁體中文：依序寫清**過程與結果**、感官與關鍵反應；可白話帶到五維誰升誰降（不寫具體數字）。
空一行後，另起一行以「【現狀】」開頭（含括號），下接**長段**繁體中文：人在哪、周遭張力、NPC 或自我的心緒，並**穿插**「【世界觀碎片】」或「【遠處的異動】」至少其一（見【敘事擴張】）；段落內可分段換行，保持易讀。
空一行後，以「【抉擇】」為小標，下列恰好 4 行「1. …」到「4. …」；選項要**短而有力**，像「1. 賠禮道歉」「2. 拔劍就砍」這種也可以；每句繁體，**約 4–18 字**為宜（可更短若意思清楚），四條路線要分得開，禁止「繼續／休息一下」敷衍項。**若當前為平凡都市位面**（見系統座標），【抉擇】須符合【平凡都市｜敘事優先】之情感／生活向比例；專屬選項可改為生活道具、信物、同伴陪行等，不必全為戰鬥。

【動態選項解鎖】每回合【抉擇】四行中，**恰好 1–2 行**必須是「專屬選項」：在序號與內文之間插入**半形**標籤，格式如「1. [物品] …」「2. [武學] …」「3. [同伴] …」「4. [消耗能源] …」。標籤方括號內須含關鍵字：**物品**、**武學**、**同伴**、**消耗**（消耗類可寫 [消耗靈石]、[消耗道行] 等）。**專屬選項只能**使用【動態選項解鎖｜資源掃描】與上文系統快照中**確實存在**的道具（含已裝備者）、武學條目與同伴；缺資源則勿生成該類標籤，改寫其他路線。範例（僅示格式）：「2. [物品] 使用「電漿手槍」火力壓制」「3. [武學] 施展「碎風斬」強行破陣」「4. [同伴] 指揮「阿斯塔特修士」掩護射界」。
【消耗與後端聯動】凡含 **[消耗…]** 的選項：若扣行囊堆疊，該行**必須**含一對直角引號「」包住**與 inventory 完全一致**的道具全名，以便玩家點選後由後端自動扣量；若扣靈石／白銀／信用點，該行必須寫出明確數量（如 靈石 x2、白銀 50、信用點 120）。**[消耗道行]** 會觸發後端扣減道行屬性。
強力招式、術式、高科技武裝等 **[消耗]** 行須標明 **能量 xN**（或真氣／魔力／電力／體力等同義詞＋數字）；未標數字時後端仍會扣預設能量。玩家 **energy 歸零** 時進入虛弱：高階招式應失敗率高、後果慘烈，直至敘事中回復並在 JSON 調高 energy.current。
【戰果基準】玩家若擁有並動用明顯匹配的**高階裝備、高明武學或可用同伴**，【因果結算】應合理偏向**大獲全勝、完走下風、低損**；若**赤手空拳、無對應武學或裝備卻硬拚同階危機**，應偏向**慘勝、重傷、反噬、裝備受損**等，並反映於 status、[ATTR] 與 JSON。

機械規則：
1. 每次回覆只能是「一則完整 JSON 物件」，禁止 markdown、禁止程式碼區塊、禁止 JSON 外任何文字。
2. "narrative"（繁體中文）：區塊順序嚴格為「【因果結算】→【現狀】→【抉擇】」；面板時前兩段可極短，仍須含【抉擇】四行。
3. 在「4. …」選項行之後（可空一行），必須另起一行且僅此行含數值，格式嚴格為：
   [ATTR: 道行=整數, 理智=整數, 賽博強化=整數, 業力=整數, 壽元=整數]
   整數範圍 1–100；五鍵名須與上完全一致。本回合依劇情合理更新五維（與上一回合相比單鍵變化宜 ≤ 8，除非敘事有重大轉折）。
4. 可選鍵（若本回合有更新則給出，否則省略）："cultivation"、"sanity"、"cyber_aug"、"karma"、"lifespan" 可作為與 [ATTR] 一致的冗餘數值（可省略，以 [ATTR] 為準）。
5. 可選："inventory" 必須為物件：含 "currencies"（silver、spirit_stone、credit）與 "items"（各物含 id、name、category: consumable|material|equipment、quantity、equipped、description）。劇情中若授予物品，請寫「你獲得了「物品名」」等句式，並同步更新 inventory；授予貨幣可寫「白銀 200」「靈石 x3」「信用點 +500」等以利後端解析。
6. 可選："skills"：物件含 "entries" 陣列（各筆 id、name、category: external|internal|heart|augment、realm、realm_tier（low|mid|high）、description、fusion_percent 僅改造；**可選** grade（黃階／玄階／地階／天階／亞空間禁忌）、origin（player＝玩家自創））。**每回合結算若涉及修為／招式／改造，必對照上一回合武學一覽並更新**；授予或提升可寫「你領悟了「名稱」」「自創了「名稱」」並寫入 JSON；開悟或自創演化時須明確寫出哪一條目晉階。改造條請維持敘事與 fusion_percent 一致。
7. 可選："companions"：必須為物件，鍵 "version"（固定 1）、"entries"（陣列）。每名同伴一筆物件，鍵須齊備：
   "id"（英數底線短字串，同一人物跨回合不變；新登場若缺則你自擬如 cp_a1b2）、"name"（名號，可含間隔號）、"title"（頭銜一行）、"race_or_faction"（種族／陣營）、"favor_percent"（0–100 整數，表因果羈絆／信任）、"status"（當前狀態短語：如 重傷、狂熱、契約反噬、冷定 等）、"backstory"（≤90 字背景）、"visual"（≤48 字，僅畫面與氣質一線，供介面呈現，勿與 backstory 重複堆砌）、"signature_ability"（≤56 字，戰鬥或奇遇時可動用的異質手段，須符合萬界融合）、"memory_note"（可選，≤60 字，本回合後可更新的短期記憶或誓言殘句）。
   當玩家與 NPC 締結同行、結伴、契約隨行、收留殘魂、押為人質轉同伴等敘事成立時，本回合必須在 companions.entries 新增或更新該員（以 id 或 name 合併，勿重複建檔）；好感與 status 須隨劇情波動。
   【萬界融合｜同伴名號範式】稱號可混搭兩界以上，虛構專名、勿照搬現實作品全名；例：「寂靜修女·葉孤城」（不碰亞空間的劍手）、「賽博家政精靈·多比」（家務機體硬塞戰鬥晶片）、「深潛者道士」（海底練邪門功法的）。backstory、visual、signature_ability 用**短白話**寫清即可，不要長篇設定表。
   戰鬥、追逐、儀式、巢都異常與亞空間餘波中，敘事必須考量 entries 內每名同伴之在場與否、狀態與 signature_ability（可寫其失誤、代價、與玩家因果互噬），禁止「同伴消失」式忽略。
8. 可選："quests"：物件或陣列，精煉即可。
9. 必備："current_location"：物件，鍵 "plane"、"world"、"address"（皆為繁體短字串，各≤120字）。**每回合回覆都必須輸出**，且三欄會**原樣顯示於玩家側邊欄**，故必須全為架空專名；**嚴禁**現實國名、城市、行政區、街巷、港灣、島嶼、地標、站名、機場等（含各語言與別稱），同【禁令｜現實地理】。若角色未離開原地，三者可与上一回合相同；若經傳送門、艦跳、界門、墜入裂隙等，必須同步改寫對應層級。若 plane 為「平凡都市」或含該意象，address 須依【平凡都市｜位面專則】使用虛構都市地點。
10. 必備："status"、"mental_state"；**建議**輸出 "rank"（繁體各≤48字）。"rank" 僅敘事對照，**介面境界由 skills 推算**。"status"＝肉身／處境；"mental_state"＝理智與靈魂穩定度，**須與本回合 [ATTR] 理智及劇情聯動**。**勿**與 companions.entries[].status 混淆。
11. **建議每回合輸出 "energy"**：物件，鍵 "current"、"max"（非負整數，current≤max）。休息、冥想、換電池／充能、服藥回氣等敘事成立時**提高 current**；**勿**在 JSON 內重複扣除玩家本回合已點選之 **[消耗]** 抉擇所標「能量 xN」（該扣減由後端執行）。energy 歸零時 status 可能帶「虛弱／枯竭」後果，須在【因果結算】寫出行動艱難與高階招式不可用或極易失手。
12. 不要重複貼上上一回合全文；不要解釋 JSON。"""

# 回合 API：短期上下文條數與長期壓縮門檻
MEMORY_SHORT_SEND = 5
MEMORY_COMPRESS_THRESHOLD = 15
MEMORY_KEEP_AFTER_COMPRESS = 5
UPDATE_STATE_TAG = "[UPDATE_STATE]"

MASTER_PROMPT_SLIM = """【刃界錄｜回合用緊湊規則】
你是繁體中文文字 RPG 敘事 GM：白話、具體、可畫面化；禁止現實國名／城市／行政區／著名地標等（須完全架空專名）。
每回合僅輸出「一則完整 JSON 物件」（禁止 markdown、禁止 JSON 外文字），含 narrative（【因果結算】【現狀】【抉擇】恰好四選項）、行末 [ATTR: 道行=整數, 理智=整數, 賽博強化=整數, 業力=整數, 壽元=整數]、current_location、status、mental_state、rank；並視劇情更新 inventory、skills、companions、quests、energy 等（鍵名與結構須與《刃界錄》主規則一致，細節你自行依上下文補齊）。
若劇情涉及**獲得物品、人際變動、位置移動、境界突破或重大轉折**，JSON 結束後另起一行追加（供後端解析並自玩家畫面移除）：[UPDATE_STATE] 後接**單一** JSON 物件，可含 "identity"（鍵：名號、境界、目前裝備；舊鍵 "character" 亦可）、"inventory"（字串陣列，新獲武器／道具名）、"relationships"（NPC→身份或好感描述）、"location"（單行精確位置）、"milestones"（字串陣列，本回合重大劇情節點）、"summary"（併入【劇情前情提要】的短片段）。無變更可省略 [UPDATE_STATE]。"""


PANEL_USER_LINES: dict[str, str] = {
    "inventory": (
        "【面板｜行囊】請用白話精簡條列：現在有哪些道具、武器、材料，狀態怎樣。"
        "真的空的就直說行囊空。"
    ),
    "martial": (
        "【面板｜武學】請用白話精簡條列：功法、改造、招式進度（沒有就說沒有）。"
    ),
    "companions": (
        "【面板｜同伴】系統已附 companions.entries 快照。請用白話條列每位同伴：名號、頭銜、種族／陣營、羈絆％、狀態、長相一句、拿手招一句；"
        "若劇情有變就在 JSON 的 companions 裡改 favor_percent、status、memory_note。沒同伴就說獨行。"
    ),
    "quests": (
        "【面板｜任務】請用白話精簡條列：主線在幹嘛、支線有沒有（沒有就說空白）。"
    ),
}

QUESTS_MODEL_VERSION = 2
QUEST_EVOLVE_ACTION_THRESHOLD = 10

QUEST_SHELVE_EVOLVE_SYSTEM = """你是《刃界錄》因果編年史官。只做一件事：依輸入中「擱置」任務與當前座標／時間刻度，推演這些因果在玩家疏於照顧、跨位面或光陰流逝後如何自行發展。
規則：嚴禁現實地名；繁體中文；可出現惡化、死亡、墮落、陣營翻轉、功敗垂成等，須能接上原任務意向。
輸出「一則 JSON 物件」且僅此物件，禁止 markdown。鍵 "entries"：陣列，每筆必含 id（與輸入完全相同）、title（任務名號，可改寫）、description（當前因果進度或結局描述）、branch（字串 main 或 side）、status（字串 shelved）、causal_shift（布林，相對原條目若屬重大變化則 true）。"""


def _make_quest_entry(
    *, title: str, description: str, branch: str, status: str = "tracking"
) -> dict[str, Any]:
    return _normalize_one_quest_entry(
        {
            "id": f"q_{secrets.token_hex(4)}",
            "title": title,
            "description": description,
            "branch": branch,
            "status": status,
            "shelved_at_tick": None,
            "causal_shift": False,
        }
    )


def _normalize_one_quest_entry(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        t = raw.strip()
        return _make_quest_entry(title="因果條目", description=t[:2000], branch="side")
    if not isinstance(raw, dict):
        return _make_quest_entry(
            title="因果條目", description=str(raw)[:2000], branch="side"
        )
    tid = str(raw.get("id") or "").strip() or f"q_{secrets.token_hex(4)}"
    branch = str(raw.get("branch") or raw.get("type") or "side").lower()
    if branch not in ("main", "side"):
        branch = "side"
    status = str(raw.get("status") or "tracking").lower()
    if status not in ("tracking", "shelved"):
        status = "tracking"
    st_at = raw.get("shelved_at_tick")
    try:
        st_at_i = int(st_at) if st_at is not None else None
    except (TypeError, ValueError):
        st_at_i = None
    return {
        "id": tid[:64],
        "title": (str(raw.get("title") or raw.get("name") or "未命名因果"))[:200],
        "description": (
            str(
                raw.get("description")
                or raw.get("detail")
                or raw.get("progress")
                or ""
            )
        )[:2000],
        "branch": branch,
        "status": status,
        "shelved_at_tick": st_at_i,
        "causal_shift": bool(raw.get("causal_shift")),
    }


def normalize_quests_model(raw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": QUESTS_MODEL_VERSION,
        "player_action_tick": 0,
        "last_evolution_tick": 0,
        "entries": [],
    }
    if raw is None:
        return base
    if isinstance(raw, dict):
        if isinstance(raw.get("entries"), list):
            base["player_action_tick"] = max(0, int(raw.get("player_action_tick") or 0))
            base["last_evolution_tick"] = max(
                0, int(raw.get("last_evolution_tick") or 0)
            )
            base["entries"] = [
                _normalize_one_quest_entry(x) for x in raw["entries"]
            ]
            base["version"] = QUESTS_MODEL_VERSION
            return base
        entries: list[dict[str, Any]] = []
        for key, br, title_hint in (
            ("main", "main", "主線因果"),
            ("主線", "main", "主線因果"),
            ("primary", "main", "主線因果"),
            ("side", "side", "支線因果"),
            ("支線", "side", "支線因果"),
        ):
            v = raw.get(key)
            if v and str(v).strip():
                entries.append(
                    _make_quest_entry(
                        title=title_hint,
                        description=str(v).strip()[:2000],
                        branch=br,
                    )
                )
        if entries:
            base["entries"] = entries
            return base
        return base
    if isinstance(raw, list):
        base["entries"] = [_normalize_one_quest_entry(x) for x in raw]
        return base
    return base


def _upsert_quest_entry(entries: list[dict[str, Any]], e: dict[str, Any]) -> None:
    eid = e.get("id")
    for i, x in enumerate(entries):
        if x.get("id") == eid:
            entries[i] = e
            return
    entries.append(e)


def merge_ai_quest_payload(cur: dict[str, Any], inc: Any) -> dict[str, Any]:
    out = {
        "version": QUESTS_MODEL_VERSION,
        "player_action_tick": int(cur.get("player_action_tick") or 0),
        "last_evolution_tick": int(cur.get("last_evolution_tick") or 0),
        "entries": list(cur.get("entries") or []),
    }
    if inc is None:
        return out
    if isinstance(inc, dict) and isinstance(inc.get("entries"), list):
        for item in inc["entries"]:
            _upsert_quest_entry(out["entries"], _normalize_one_quest_entry(item))
        return out
    if isinstance(inc, list):
        for item in inc:
            if isinstance(item, str) and item.strip():
                _upsert_quest_entry(
                    out["entries"],
                    _make_quest_entry(
                        title="因果條目",
                        description=item.strip()[:2000],
                        branch="side",
                    ),
                )
            elif isinstance(item, dict):
                _upsert_quest_entry(out["entries"], _normalize_one_quest_entry(item))
        return out
    if isinstance(inc, dict):
        merged_any = False
        for key, br, title_hint in (
            ("main", "main", "主線因果"),
            ("主線", "main", "主線因果"),
            ("side", "side", "支線因果"),
            ("支線", "side", "支線因果"),
        ):
            v = inc.get(key)
            if v and str(v).strip():
                _upsert_quest_entry(
                    out["entries"],
                    _make_quest_entry(
                        title=title_hint,
                        description=str(v).strip()[:2000],
                        branch=br,
                    ),
                )
                merged_any = True
        if not merged_any and inc:
            _upsert_quest_entry(
                out["entries"],
                _make_quest_entry(
                    title="因果摘要",
                    description=json.dumps(inc, ensure_ascii=False)[:2000],
                    branch="side",
                ),
            )
    return out


def merge_quests_into_state(game_state: dict[str, Any], inc: Any) -> None:
    cur = normalize_quests_model(game_state.get("quests"))
    game_state["quests"] = merge_ai_quest_payload(cur, inc)


def bump_quest_action_tick(game_state: dict[str, Any]) -> None:
    q = normalize_quests_model(game_state.get("quests"))
    q["player_action_tick"] = int(q.get("player_action_tick") or 0) + 1
    game_state["quests"] = q


def _has_shelved_quests(entries: list[dict[str, Any]]) -> bool:
    return any(e.get("status") == "shelved" for e in entries)


async def evolve_shelved_quests_with_ai(game_state: dict[str, Any]) -> bool:
    q = normalize_quests_model(game_state.get("quests"))
    game_state["quests"] = q
    shelved = [e for e in q["entries"] if e.get("status") == "shelved"]
    if not shelved:
        return False
    loc = normalize_current_location(game_state.get("current_location"))
    payload = {
        "current_location": loc,
        "player_action_tick": q["player_action_tick"],
        "shelved_quests": [
            {
                "id": e["id"],
                "title": e["title"],
                "description": e["description"],
                "branch": e["branch"],
            }
            for e in shelved
        ],
    }
    user_msg = (
        "以下為擱置中的因果條目，請依時間／位面流逝推演其後續（可惡化、反轉、終結）。\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    msgs = [
        {"role": "system", "content": QUEST_SHELVE_EVOLVE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    try:
        raw = await _call_poe(msgs, temperature=0.84)
        parsed = _extract_json_object(raw)
    except (HTTPException, ValueError):
        return False
    updated = parsed.get("entries")
    if not isinstance(updated, list):
        return False
    by_id = {e["id"]: dict(e) for e in q["entries"]}
    changed = False
    for u in updated:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id") or "").strip()
        if not uid or uid not in by_id:
            continue
        old = by_id[uid]
        if old.get("status") != "shelved":
            continue
        nt = str(u.get("title") or old["title"])[:200]
        nd = str(u.get("description") or old["description"])[:2000]
        cs = bool(u.get("causal_shift"))
        if (nt, nd) != (old.get("title"), old.get("description")):
            cs = True
        br_raw = str(u.get("branch") or old.get("branch") or "side").lower()
        br = br_raw if br_raw in ("main", "side") else str(old.get("branch") or "side")
        by_id[uid] = {
            **old,
            "title": nt,
            "description": nd,
            "causal_shift": cs,
            "status": "shelved",
            "branch": br,
        }
        changed = True
    if not changed:
        return False
    q["entries"] = list(by_id.values())
    q["last_evolution_tick"] = int(q.get("player_action_tick") or 0)
    game_state["quests"] = q
    return True


async def maybe_evolve_shelved_quests(
    game_state: dict[str, Any], *, force: bool
) -> None:
    q = normalize_quests_model(game_state.get("quests"))
    game_state["quests"] = q
    if not _has_shelved_quests(q["entries"]):
        return
    tick = int(q.get("player_action_tick") or 0)
    last = int(q.get("last_evolution_tick") or 0)
    if not force and tick - last < QUEST_EVOLVE_ACTION_THRESHOLD:
        return
    await evolve_shelved_quests_with_ai(game_state)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            data = json.loads(m.group())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise ValueError("model did not return valid JSON object")


def _merge_optional_collections(game_state: dict[str, Any], parsed: dict[str, Any]) -> None:
    if "inventory" in parsed:
        game_state["inventory"] = merge_inventory_from_model(
            game_state.get("inventory"), parsed.get("inventory")
        )
    if "skills" in parsed:
        game_state["skills"] = merge_skills_from_model(
            game_state.get("skills"), parsed.get("skills")
        )
    elif "martial_arts" in parsed and isinstance(
        parsed["martial_arts"], (dict, list)
    ):
        game_state["skills"] = merge_skills_from_model(
            game_state.get("skills"), parsed["martial_arts"]
        )
    if "companions" in parsed and parsed["companions"] is not None:
        inc = parsed["companions"]
        if isinstance(inc, (dict, list)):
            game_state["companions"] = merge_companions_from_model(
                game_state.get("companions"), inc
            )
    if "quests" in parsed:
        merge_quests_into_state(game_state, parsed.get("quests"))
    merge_current_location_into_state(game_state, parsed)
    merge_energy_into_state(game_state, parsed)
    merge_rank_status_into_state(game_state, parsed)
    enforce_mundane_world_label(game_state)


def _merge_panel_optional(game_state: dict[str, Any], parsed: dict[str, Any]) -> None:
    if "inventory" in parsed:
        game_state["inventory"] = merge_inventory_from_model(
            game_state.get("inventory"), parsed.get("inventory")
        )
    if "skills" in parsed:
        game_state["skills"] = merge_skills_from_model(
            game_state.get("skills"), parsed.get("skills")
        )
    elif "martial_arts" in parsed and isinstance(
        parsed["martial_arts"], (dict, list)
    ):
        game_state["skills"] = merge_skills_from_model(
            game_state.get("skills"), parsed["martial_arts"]
        )
    if "companions" in parsed and parsed["companions"] is not None:
        inc = parsed["companions"]
        if isinstance(inc, (dict, list)):
            game_state["companions"] = merge_companions_from_model(
                game_state.get("companions"), inc
            )
    if "quests" in parsed:
        merge_quests_into_state(game_state, parsed.get("quests"))
    merge_current_location_into_state(game_state, parsed)
    merge_energy_into_state(game_state, parsed)
    merge_rank_status_into_state(game_state, parsed)
    enforce_mundane_world_label(game_state)
    apply_derived_rank_from_skills(game_state)
    apply_energy_max_from_skill_weights(game_state)
    clamp_and_sync_energy(game_state)


def _ensure_game_state_shape(game_state: dict[str, Any]) -> None:
    if "messages" not in game_state or not isinstance(game_state["messages"], list):
        game_state["messages"] = []
    if "stats" not in game_state or not isinstance(game_state["stats"], dict):
        game_state["stats"] = _default_five_stats()
    else:
        st = game_state["stats"]
        if not all(k in st for k in FIVE_STAT_KEYS):
            migrated = _default_five_stats()
            if isinstance(st.get("sanity"), (int, float)):
                try:
                    migrated["sanity"] = max(1, min(100, int(st["sanity"])))
                except (TypeError, ValueError):
                    pass
            game_state["stats"] = migrated
        else:
            merged = _default_five_stats()
            for k in FIVE_STAT_KEYS:
                if k not in st:
                    continue
                try:
                    merged[k] = max(1, min(100, int(st[k])))
                except (TypeError, ValueError):
                    pass
            game_state["stats"] = merged
    inv = game_state.get("inventory")
    if not isinstance(inv, dict):
        game_state["inventory"] = _default_inventory()
    else:
        game_state["inventory"] = normalize_inventory(inv)
    game_state["skills"] = normalize_skills(
        game_state.get("skills"), game_state.get("martial_arts")
    )
    if "martial_arts" not in game_state or not isinstance(
        game_state["martial_arts"], dict
    ):
        game_state["martial_arts"] = {}
    game_state["companions"] = normalize_companions(game_state.get("companions"))
    game_state["quests"] = normalize_quests_model(game_state.get("quests"))
    game_state["current_location"] = normalize_current_location(
        game_state.get("current_location")
    )
    enforce_mundane_world_label(game_state)
    if "last_narrative" not in game_state:
        game_state["last_narrative"] = ""
    if "opening_tags" not in game_state:
        game_state["opening_tags"] = []
    game_state["energy"] = normalize_energy(game_state.get("energy"))
    _ensure_rank_status_shape(game_state)
    apply_derived_rank_from_skills(game_state)
    apply_energy_max_from_skill_weights(game_state)
    clamp_and_sync_energy(game_state)
    _ensure_core_save(game_state)


def _default_core_save(player_name: str) -> dict[str, Any]:
    nm = (player_name or "").strip() or "—"
    return {
        "identity": {"名號": nm, "境界": "練氣守一", "目前裝備": "無"},
        "inventory": [],
        "relationships": {},
        "location": "晨曦市·咖啡廳",
        "milestones": [],
        "summary": "",
    }


def _migrate_permanent_state_to_core_save(game_state: dict[str, Any]) -> None:
    perm = game_state.get("permanent_state")
    if not isinstance(perm, dict):
        return
    cs = _default_core_save("—")
    ch = perm.get("character")
    if isinstance(ch, dict):
        for k in ("名號", "境界", "目前裝備"):
            if k in ch and str(ch.get(k) or "").strip():
                cs["identity"][k] = str(ch[k]).strip()[:200]
    if isinstance(perm.get("inventory"), list):
        cs["inventory"] = [str(x).strip()[:200] for x in perm["inventory"] if str(x).strip()]
    if isinstance(perm.get("relationships"), dict):
        cs["relationships"] = dict(perm["relationships"])
    if isinstance(perm.get("location"), str) and perm["location"].strip():
        cs["location"] = perm["location"].strip()[:500]
    if isinstance(perm.get("summary"), str):
        cs["summary"] = perm["summary"][:8000]
    game_state["core_save"] = cs
    del game_state["permanent_state"]


def _ensure_core_save(game_state: dict[str, Any]) -> None:
    if isinstance(game_state.get("permanent_state"), dict):
        _migrate_permanent_state_to_core_save(game_state)
    base = _default_core_save("—")
    if not isinstance(game_state.get("core_save"), dict):
        game_state["core_save"] = base.copy()
    cs = game_state["core_save"]
    id0 = base["identity"]
    idt = cs.get("identity")
    if not isinstance(idt, dict):
        cs["identity"] = id0.copy()
    else:
        for kk, vv in id0.items():
            if kk not in idt:
                idt[kk] = vv
    if not isinstance(cs.get("inventory"), list):
        cs["inventory"] = []
    if not isinstance(cs.get("relationships"), dict):
        cs["relationships"] = {}
    if "location" not in cs or not isinstance(cs["location"], str):
        cs["location"] = base["location"]
    if not isinstance(cs.get("milestones"), list):
        cs["milestones"] = []
    if "summary" not in cs or not isinstance(cs["summary"], str):
        cs["summary"] = ""
    loc = normalize_current_location(game_state.get("current_location"))
    if (
        cs["location"] == base["location"]
        and loc.get("plane") not in (None, "", "未定")
    ):
        parts = [loc["plane"], loc["world"], loc["address"]]
        joined = "·".join(p for p in parts if p and str(p).strip() and str(p) != "未定")
        if joined:
            cs["location"] = joined[:500]


def _combined_inventory_truth_lines(game_state: dict[str, Any]) -> str:
    cs = game_state.get("core_save") or {}
    acc: list[str] = []
    for x in cs.get("inventory") or []:
        s = str(x).strip()
        if s and s not in acc:
            acc.append(s)
    inv = game_state.get("inventory") or {}
    if isinstance(inv, dict):
        for it in inv.get("items") or []:
            if isinstance(it, dict):
                n = str(it.get("name") or "").strip()
                if n and n not in acc:
                    acc.append(n)
    return "、".join(acc) if acc else "（空）"


def _relationships_truth_lines(rel: Any) -> str:
    if not isinstance(rel, dict) or not rel:
        return "（尚無）"
    parts: list[str] = []
    for k, v in list(rel.items())[:32]:
        parts.append(f"{k}：{v}")
    return "；".join(parts)


def _milestones_truth_lines(ms: Any) -> str:
    if not isinstance(ms, list) or not ms:
        return "（尚無）"
    parts: list[str] = []
    for x in ms[:24]:
        s = str(x).strip()
        if s:
            parts.append(s)
    return "；".join(parts) if parts else "（尚無）"


def format_absolute_lock_block(game_state: dict[str, Any]) -> str:
    """【絕對鎖定區】僅含永恆欄位；前情提要另段注入，對話裁切不會動到此區資料。"""
    _ensure_core_save(game_state)
    cs = game_state["core_save"]
    idt = cs["identity"]
    name = str(idt.get("名號", "—"))
    jing = str(idt.get("境界", "—"))
    equip = str(idt.get("目前裝備", "無"))
    loc = str(cs.get("location", ""))
    inv_s = _combined_inventory_truth_lines(game_state)
    rel_s = _relationships_truth_lines(cs.get("relationships"))
    ms_s = _milestones_truth_lines(cs.get("milestones"))
    return (
        "【當前宇宙唯一真理 - AI 必須嚴格遵守】\n"
        f"玩家名號：{name} | 當前境界：{jing}\n"
        f"所在位置：{loc}\n"
        f"目前裝備：{equip}\n"
        f"行囊清單：{inv_s}\n"
        f"重要人際關係：{rel_s}\n"
        f"劇情里程碑：{ms_s}"
    )


def build_final_prompt_for_turn(game_state: dict[str, Any], player_name: str) -> str:
    _ensure_core_save(game_state)
    idt = game_state["core_save"]["identity"]
    pn = (player_name or "").strip()
    if pn:
        idt["名號"] = pn
    lock = format_absolute_lock_block(game_state)
    summary = (game_state["core_save"].get("summary") or "").strip() or "（無）"
    preface = f"【劇情前情提要】\n{summary}"
    geo = FICTIONAL_GEO_ANCHOR.split("\n")[0].strip()
    return f"{lock}\n\n{preface}\n\n{geo}\n\n{MASTER_PROMPT_SLIM}"


def _split_update_state(raw: str) -> tuple[str, dict[str, Any] | None]:
    s = raw.strip()
    tag = UPDATE_STATE_TAG
    i = s.rfind(tag)
    if i < 0:
        return s, None
    main = s[:i].strip()
    tail = s[i + len(tag) :].strip()
    if not tail:
        return main, None
    try:
        patch = json.loads(tail)
    except json.JSONDecodeError:
        try:
            patch = _extract_json_object(tail)
        except ValueError:
            return main, None
    if not isinstance(patch, dict):
        return main, None
    return main, patch


def _apply_location_string_to_current(game_state: dict[str, Any], loc_line: str) -> None:
    line = loc_line.strip()
    if not line:
        return
    parts = [p.strip() for p in line.split("·") if p.strip()]
    if len(parts) >= 3:
        game_state["current_location"] = normalize_current_location(
            {"plane": parts[0], "world": parts[1], "address": "·".join(parts[2:])[:120]}
        )
    elif len(parts) == 2:
        game_state["current_location"] = normalize_current_location(
            {"plane": parts[0], "world": parts[1], "address": parts[1]}
        )
    else:
        cur = normalize_current_location(game_state.get("current_location"))
        cur["address"] = parts[0][:120]
        game_state["current_location"] = cur
    enforce_mundane_world_label(game_state)


def _apply_core_save_patch(
    game_state: dict[str, Any], patch: dict[str, Any] | None
) -> None:
    if not patch:
        return
    _ensure_core_save(game_state)
    cs = game_state["core_save"]
    id_patch = patch.get("identity")
    if id_patch is None and isinstance(patch.get("character"), dict):
        id_patch = patch["character"]
    if isinstance(id_patch, dict):
        idt = cs["identity"]
        for kk, vv in id_patch.items():
            if isinstance(vv, str) and vv.strip():
                idt[str(kk)] = vv.strip()[:200]
        jing = idt.get("境界")
        if isinstance(jing, str) and jing.strip():
            game_state["rank"] = jing.strip()[:48]
    if "inventory" in patch and isinstance(patch["inventory"], list):
        for x in patch["inventory"]:
            s = str(x).strip()
            if s and s not in cs["inventory"]:
                cs["inventory"].append(s[:200])
    if "relationships" in patch and isinstance(patch["relationships"], dict):
        for kk, vv in patch["relationships"].items():
            cs["relationships"][str(kk)[:120]] = str(vv)[:500]
    if "location" in patch and isinstance(patch["location"], str) and patch["location"].strip():
        cs["location"] = patch["location"].strip()[:500]
        _apply_location_string_to_current(game_state, cs["location"])
    if "milestones" in patch and isinstance(patch["milestones"], list):
        for x in patch["milestones"]:
            s = str(x).strip()
            if s and s not in cs["milestones"]:
                cs["milestones"].append(s[:500])
        cs["milestones"] = cs["milestones"][-64:]
    if "summary" in patch and isinstance(patch["summary"], str) and patch["summary"].strip():
        add = patch["summary"].strip()
        if cs["summary"].strip():
            cs["summary"] = (cs["summary"].rstrip() + "\n" + add)[:8000]
        else:
            cs["summary"] = add[:8000]


async def _maybe_compress_message_history(game_state: dict[str, Any]) -> None:
    hist = game_state.get("messages")
    if not isinstance(hist, list) or len(hist) <= MEMORY_COMPRESS_THRESHOLD:
        return
    to_fold = hist[:-MEMORY_KEEP_AFTER_COMPRESS]
    keep = hist[-MEMORY_KEEP_AFTER_COMPRESS:]
    if not to_fold:
        return
    _ensure_core_save(game_state)
    cs = game_state["core_save"]
    existing = (cs.get("summary") or "").strip()
    dialogue_parts: list[str] = []
    for m in to_fold:
        role = str(m.get("role", ""))
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        dialogue_parts.append(f"{role}：{content[:6000]}")
    dialogue = "\n\n---\n\n".join(dialogue_parts)
    sys_m = (
        "你是編輯。將【待合併對話】濃縮為繁體中文「劇情前情提要」，"
        "承接【既有提要】、不重複客套，400 字內，只輸出提要正文。"
    )
    user_m = f"【既有提要】\n{existing or '（無）'}\n\n【待合併對話】\n{dialogue}"
    try:
        summary_new = await _call_poe(
            [
                {"role": "system", "content": sys_m},
                {"role": "user", "content": user_m},
            ],
            temperature=0.35,
        )
        t = summary_new.strip()
        if t:
            if existing:
                cs["summary"] = (existing + "\n\n" + t)[:8000]
            else:
                cs["summary"] = t[:8000]
    except HTTPException:
        cs["summary"] = (
            (existing + "\n\n（舊對話已裁切；摘要生成失敗）") if existing else "（舊對話已裁切；摘要生成失敗）"
        )[:8000]
    game_state["messages"] = keep


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), 310_000
    )
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hx = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), 310_000
        )
        return secrets.compare_digest(dk.hex(), hx)
    except (ValueError, AttributeError):
        return False


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                player_name TEXT NOT NULL,
                game_state TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            """
        )


def get_user_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT u.id, u.username, u.player_name, u.game_state "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    ).fetchone()
    return row


def save_user_game_state(conn: sqlite3.Connection, user_id: int, game_state: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE users SET game_state = ? WHERE id = ?",
        (json.dumps(game_state, ensure_ascii=False), user_id),
    )


class RegisterBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    player_name: str = Field(..., min_length=1, max_length=32)


class LoginBody(BaseModel):
    username: str
    password: str


class TurnBody(BaseModel):
    choice: str = Field(..., min_length=1, max_length=4000)


class PanelBody(BaseModel):
    panel: Literal["inventory", "martial", "companions", "quests"]


class InventoryActionBody(BaseModel):
    action: Literal["use", "equip", "unequip"]
    item_id: str = Field(..., min_length=2, max_length=96)


class SkillActionBody(BaseModel):
    action: Literal["cultivate", "abandon"]
    skill_id: str = Field(..., min_length=2, max_length=96)


class RenameBody(BaseModel):
    player_name: str = Field(..., min_length=1, max_length=32)


class RealmShuttleBody(BaseModel):
    realm: str = Field(..., min_length=2, max_length=48)


class QuestTrackBody(BaseModel):
    quest_id: str = Field(..., min_length=2, max_length=64)
    action: Literal["shelve", "resume"]


app = FastAPI(title="Blade-style Text RPG", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


def auth_user(
    authorization: Annotated[str | None, Header()] = None,
) -> tuple[int, str, str, dict[str, Any]]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="需要登入")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="需要登入")
    with get_db() as conn:
        row = get_user_by_token(conn, token)
        if row is None:
            raise HTTPException(status_code=401, detail="登入已失效，請重新登入")
        try:
            game_state = json.loads(row["game_state"])
        except json.JSONDecodeError:
            tags = pick_opening_tags()
            game_state = fallback_opening_game_state(str(row["player_name"]), tags)
            save_user_game_state(conn, int(row["id"]), game_state)
        _ensure_game_state_shape(game_state)
        return int(row["id"]), str(row["username"]), str(row["player_name"]), game_state


AuthUser = Annotated[tuple[int, str, str, dict[str, Any]], Depends(auth_user)]


async def _call_poe(messages: list[dict[str, str]], temperature: float = 0.82) -> str:
    api_key = os.getenv("POE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="缺少 POE_API_KEY：請複製 .env.example 為 .env 並填入金鑰。",
        )
    model = os.getenv("POE_MODEL", "GPT-4o").strip() or "GPT-4o"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
        "max_tokens": _poe_max_tokens(),
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                POE_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Poe 連線失敗：{e!s}") from e
    if r.status_code >= 400:
        try:
            err = r.json()
            detail = err.get("message") or err.get("error") or r.text
        except json.JSONDecodeError:
            detail = r.text or r.reason_phrase
        raise HTTPException(status_code=r.status_code, detail=str(detail)[:2000])
    try:
        completion = r.json()
        content = (
            completion.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
    except (IndexError, AttributeError, TypeError) as e:
        raise HTTPException(status_code=502, detail="Poe 回傳格式異常") from e
    if not content or not str(content).strip():
        raise HTTPException(status_code=502, detail="模型未回傳內容")
    return str(content).strip()


def _partial_narrative_for_stream_preview(buffer: str) -> str:
    """從仍可能不完整的 JSON 輸出中抽出 narrative 字串，供 SSE 即時顯示。"""
    if not buffer:
        return ""
    s = buffer
    key = '"narrative"'
    i = s.find(key)
    if i < 0:
        t = s.lstrip()
        if t and not t.startswith("{"):
            return s.rstrip()
        return ""
    j = i + len(key)
    while j < len(s) and s[j] in " \t\r\n":
        j += 1
    if j >= len(s) or s[j] != ":":
        return ""
    j += 1
    while j < len(s) and s[j] in " \t\r\n":
        j += 1
    if j >= len(s) or s[j] != '"':
        return ""
    j += 1
    out: list[str] = []
    while j < len(s):
        c = s[j]
        if c == "\\":
            if j + 1 >= len(s):
                break
            esc = s[j + 1]
            if esc == "n":
                out.append("\n")
            elif esc == "r":
                out.append("\r")
            elif esc == "t":
                out.append("\t")
            elif esc in '"\\/':
                out.append(esc)
            elif esc == "u" and j + 5 < len(s):
                hex_part = s[j + 2 : j + 6]
                try:
                    out.append(chr(int(hex_part, 16)))
                except ValueError:
                    out.append(esc)
                j += 6
                continue
            else:
                out.append(esc)
            j += 2
            continue
        if c == '"':
            break
        out.append(c)
        j += 1
    return "".join(out)


async def _call_poe_stream(
    messages: list[dict[str, str]], temperature: float = 0.82
):
    """OpenAI 相容 SSE：逐段 yield 模型輸出字串片段。"""
    api_key = os.getenv("POE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="缺少 POE_API_KEY：請複製 .env.example 為 .env 並填入金鑰。",
        )
    model = os.getenv("POE_MODEL", "GPT-4o").strip() or "GPT-4o"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "max_tokens": _poe_max_tokens(),
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                POE_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", errors="replace")
                    try:
                        err_j = json.loads(body)
                        detail = err_j.get("message") or err_j.get("error") or body
                    except json.JSONDecodeError:
                        detail = body or r.reason_phrase
                    raise HTTPException(
                        status_code=r.status_code, detail=str(detail)[:2000]
                    )
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for ch in chunk.get("choices") or []:
                        delta = ch.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield piece
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Poe 連線失敗：{e!s}") from e


async def _turn_prepare_messages(
    body: TurnBody, auth: tuple[int, str, str, dict[str, Any]]
) -> tuple[int, dict[str, Any], list[dict[str, str]], str, str]:
    user_id, _username, player_name, game_state = auth
    _ensure_game_state_shape(game_state)
    await _maybe_compress_message_history(game_state)
    bump_quest_action_tick(game_state)

    sys_ctx = build_final_prompt_for_turn(game_state, player_name)
    _cmb = custom_martial_bonus_system_block(game_state, body.choice.strip())
    if _cmb:
        sys_ctx += "\n\n" + _cmb
    msgs: list[dict[str, str]] = [
        {"role": "system", "content": sys_ctx},
    ]
    hist: list[dict[str, Any]] = [
        m
        for m in game_state["messages"]
        if m.get("role") in ("user", "assistant")
        and str(m.get("content") or "").strip()
    ]
    tail = hist[-MEMORY_SHORT_SEND:]
    for m in tail:
        msgs.append(
            {"role": str(m["role"]), "content": str(m.get("content") or "").strip()}
        )
    choice_line = f"【道號｜{player_name}】{body.choice.strip()}"
    user_content = f"【玩家行動】{choice_line}"
    msgs.append({"role": "user", "content": user_content})
    choice_stripped = body.choice.strip()
    return user_id, game_state, msgs, user_content, choice_stripped


async def _finalize_turn_from_model_raw(
    user_id: int,
    raw: str,
    game_state: dict[str, Any],
    user_content: str,
    choice_stripped: str,
) -> dict[str, Any]:
    raw_body, patch = _split_update_state(raw)
    prev_stats = dict(game_state["stats"])
    try:
        parsed = _extract_json_object(raw_body)
    except ValueError:
        parsed = {"narrative": raw_body}

    narrative = parsed.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        narrative = raw_body

    narrative_raw = str(narrative).strip()
    clean_nar, stats = _merge_narrative_attrs_into_stats(
        narrative_raw, parsed, prev_stats
    )
    raw_stored = _rewrite_assistant_raw_with_clean_narrative(raw_body, clean_nar)

    game_state["messages"].append({"role": "user", "content": user_content})
    game_state["messages"].append({"role": "assistant", "content": raw_stored})
    if len(game_state["messages"]) > 48:
        game_state["messages"] = game_state["messages"][-48:]
    game_state["stats"] = stats
    game_state["last_narrative"] = clean_nar
    _merge_optional_collections(game_state, parsed)
    game_state["inventory"] = parse_loot_from_narrative(
        clean_nar, game_state["inventory"]
    )
    game_state["skills"] = parse_skill_learn_from_narrative(
        clean_nar, game_state["skills"]
    )
    apply_turn_choice_consumption(game_state, choice_stripped)
    apply_derived_rank_from_skills(game_state)
    apply_energy_max_from_skill_weights(game_state)
    clamp_and_sync_energy(game_state)
    loc = normalize_current_location(game_state.get("current_location"))
    joined = "·".join(
        p
        for p in (loc["plane"], loc["world"], loc["address"])
        if p and str(p).strip() and str(p) != "未定"
    )
    if joined:
        _ensure_core_save(game_state)
        game_state["core_save"]["location"] = joined[:500]
    _apply_core_save_patch(game_state, patch)

    await maybe_evolve_shelved_quests(game_state, force=False)

    with get_db() as conn:
        save_user_game_state(conn, user_id, game_state)

    return {
        "narrative": clean_nar,
        "stats": stats,
        "assistant_raw": raw_stored,
        "game_state": game_state,
    }


async def build_fresh_opening_state(
    player_name: str, past_life_echo: dict[str, Any] | None = None
) -> dict[str, Any]:
    """註冊／兵解時生成全新隨機開局（含 Poe 或 fallback）。兵解時可帶前世殘響以觸發宿世慧根。"""
    tags = pick_opening_tags()
    try:
        return await generate_opening_game_state(player_name, tags, past_life_echo)
    except HTTPException:
        raise
    except Exception:
        return fallback_opening_game_state(player_name, tags, past_life_echo)


async def generate_opening_game_state(
    player_name: str,
    tags: list[str],
    past_life_echo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user_msg = (
        f"道號：{player_name}\n"
        f"本次抽中的萬界標籤（須全部融入開場，並讓其中 2–3 個意象緊密交織）：{'、'.join(tags)}\n"
        "請生成開局。"
    )
    if past_life_echo:
        user_msg += (
            "\n\n【宿世慧根｜系統指令】此開局為兵解輪迴後新世。前世殘響（僅供你編劇參考）："
            f"心法層次峰值={past_life_echo.get('peak_heart_tier', 0)}、"
            f"諸藝最高層={past_life_echo.get('peak_any_tier', 0)}、"
            f"改造曾達融合度約 {past_life_echo.get('max_augment_fusion', 0)}%。"
            "側邊欄境界由 skills 自動推算，新世多半回落；若與標籤相合，可於 narrative／skills 安排殘缺遺產、斷簡或修煉較快之暗示，**禁止**開局即滿級神功。"
        )
    msgs = [
        {
            "role": "system",
            "content": FICTIONAL_GEO_ANCHOR + "\n\n" + OPENING_GENERATOR_SYSTEM,
        },
        {"role": "user", "content": user_msg},
    ]
    raw = await _call_poe(msgs, temperature=0.9)
    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        return fallback_opening_game_state(player_name, tags, past_life_echo)
    narrative = parsed.get("narrative")
    if not isinstance(narrative, str) or len(narrative.strip()) < 60:
        return fallback_opening_game_state(player_name, tags, past_life_echo)
    for mark in ("【當前處境】", "【初始裝備】", "【第一道宿命因果】"):
        if mark not in narrative:
            return fallback_opening_game_state(player_name, tags, past_life_echo)
    narrative_raw = str(narrative).strip()
    clean_nar, stats = _merge_narrative_attrs_into_stats(
        narrative_raw, parsed, _default_five_stats()
    )
    raw_stored = _rewrite_assistant_raw_with_clean_narrative(raw, clean_nar)
    gs = empty_game_state()
    gs["opening_tags"] = list(tags)
    gs["messages"] = [{"role": "assistant", "content": raw_stored}]
    gs["last_narrative"] = clean_nar
    gs["stats"] = stats
    _merge_optional_collections(gs, parsed)
    gs["inventory"] = parse_loot_from_narrative(clean_nar, gs["inventory"])
    gs["skills"] = parse_skill_learn_from_narrative(clean_nar, gs["skills"])
    loc = normalize_current_location(gs.get("current_location"))
    if loc["plane"] == "未定" and loc["world"] == "未定" and loc["address"] == "未定":
        gs["current_location"] = random_opening_location()
    _apply_reincarnation_boon(gs, past_life_echo)
    _ensure_game_state_shape(gs)
    cs = gs["core_save"]
    cs["identity"]["名號"] = player_name.strip() or "—"
    loc2 = normalize_current_location(gs.get("current_location"))
    joined = "·".join(
        p
        for p in (loc2["plane"], loc2["world"], loc2["address"])
        if p and str(p).strip() and str(p) != "未定"
    )
    if joined:
        cs["location"] = joined[:500]
    inv_cur = gs.get("inventory") or {}
    cs["inventory"] = [
        str(it.get("name"))
        for it in (inv_cur.get("items") or [])
        if isinstance(it, dict) and it.get("name")
    ]
    return gs


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        media_type="text/html; charset=utf-8",
    )


@app.get("/manifest.json")
async def manifest() -> FileResponse:
    return FileResponse(
        os.path.join(BASE_DIR, "manifest.json"),
        media_type="application/manifest+json",
    )


@app.get("/script.js")
async def script_js() -> FileResponse:
    return FileResponse(
        os.path.join(BASE_DIR, "script.js"),
        media_type="application/javascript; charset=utf-8",
    )


@app.post("/api/register")
async def register(body: RegisterBody) -> dict[str, Any]:
    u = body.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", u):
        raise HTTPException(
            status_code=400,
            detail="帳號僅能使用英數與底線，長度 3–32",
        )
    pn = body.player_name.strip()
    if not pn:
        raise HTTPException(status_code=400, detail="請填寫遊戲名號")
    ph = hash_password(body.password)
    try:
        gs = await build_fresh_opening_state(pn)
    except HTTPException:
        raise
    token = secrets.token_urlsafe(32)
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, player_name, game_state) VALUES (?,?,?,?)",
                (u, ph, pn, json.dumps(gs, ensure_ascii=False)),
            )
            uid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO sessions (token, user_id) VALUES (?,?)",
                (token, uid),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="此帳號已被使用") from None
    return {
        "token": token,
        "username": u,
        "player_name": pn,
        "game_state": gs,
        "opening_tags": gs.get("opening_tags", []),
    }


@app.post("/api/login")
async def login(body: LoginBody) -> dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, password_hash, player_name, game_state FROM users WHERE username = ?",
            (body.username.strip(),),
        ).fetchone()
        if row is None or not verify_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (token, user_id) VALUES (?,?)",
            (token, row["id"]),
        )
        try:
            game_state = json.loads(row["game_state"])
        except json.JSONDecodeError:
            tags = pick_opening_tags()
            game_state = fallback_opening_game_state(str(row["player_name"]), tags)
            save_user_game_state(conn, int(row["id"]), game_state)
        _ensure_game_state_shape(game_state)
    return {
        "token": token,
        "username": body.username.strip(),
        "player_name": row["player_name"],
        "game_state": game_state,
    }


@app.get("/api/me")
async def me(auth: AuthUser) -> dict[str, Any]:
    _uid, username, player_name, game_state = auth
    return {
        "username": username,
        "player_name": player_name,
        "game_state": game_state,
    }


@app.patch("/api/me/name")
async def rename_player(body: RenameBody, auth: AuthUser) -> dict[str, Any]:
    user_id, _u, _old, _gs = auth
    pn = body.player_name.strip()
    if not pn:
        raise HTTPException(status_code=400, detail="名號不可為空")
    with get_db() as conn:
        conn.execute("UPDATE users SET player_name = ? WHERE id = ?", (pn, user_id))
    return {"player_name": pn}


@app.post("/api/logout")
async def logout(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        with get_db() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    return {"ok": "true"}


@app.post("/api/reset_game")
@app.post("/reset_game")
async def reset_game(auth: AuthUser) -> dict[str, Any]:
    """
    兵解輪迴：依當前登入使用者，清空資料庫內該帳號之 game_state
    （含 messages 即劇情／對話記憶、屬性 stats、行囊等），
    再以萬界標籤池重新隨機組合產生全新初始劇情（嚴禁現實地名由開局提示詞約束）。
    """
    user_id, _username, player_name, old_state = auth
    echo = _past_life_echo_from_state(old_state)
    try:
        new_gs = await build_fresh_opening_state(player_name, echo)
    except HTTPException:
        raise
    with get_db() as conn:
        save_user_game_state(conn, user_id, new_gs)
    return {
        "ok": True,
        "game_state": new_gs,
        "opening_tags": new_gs.get("opening_tags", []),
    }


@app.post("/api/panel")
async def query_panel(body: PanelBody, auth: AuthUser) -> dict[str, Any]:
    user_id, _username, player_name, game_state = auth
    _ensure_game_state_shape(game_state)

    line = PANEL_USER_LINES[body.panel]
    choice_line = f"【道號｜{player_name}】{line}"
    user_content = f"【玩家行動】{choice_line}"

    msgs: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt_with_session_context(game_state)},
    ]
    for m in game_state["messages"]:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": user_content})

    raw = await _call_poe(msgs, temperature=0.78)

    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        parsed = {"narrative": raw}

    narrative = parsed.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        narrative = raw

    narrative_raw = str(narrative).strip()
    clean_nar, _ = parse_attr_block(narrative_raw)
    raw_stored = _rewrite_assistant_raw_with_clean_narrative(raw, clean_nar)

    game_state["messages"].append({"role": "user", "content": user_content})
    game_state["messages"].append({"role": "assistant", "content": raw_stored})
    if len(game_state["messages"]) > 48:
        game_state["messages"] = game_state["messages"][-48:]
    _merge_panel_optional(game_state, parsed)

    with get_db() as conn:
        save_user_game_state(conn, user_id, game_state)

    return {
        "panel": body.panel,
        "display": clean_nar,
        "game_state": game_state,
    }


@app.post("/api/inventory_action")
async def inventory_action(body: InventoryActionBody, auth: AuthUser) -> dict[str, Any]:
    user_id, _username, player_name, game_state = auth
    _ensure_game_state_shape(game_state)
    inv = normalize_inventory(game_state["inventory"])
    game_state["inventory"] = inv
    item_id = body.item_id.strip()
    idx = _find_item_index(inv["items"], item_id)
    if idx < 0:
        raise HTTPException(status_code=404, detail="找不到該物品")
    it = inv["items"][idx]

    if body.action == "equip":
        if it.get("category") != "equipment":
            raise HTTPException(status_code=400, detail="僅裝備欄可裝戴")
        if int(it.get("quantity") or 0) <= 0:
            raise HTTPException(status_code=400, detail="數量不足")
        inv["items"][idx]["equipped"] = True
        game_state["inventory"] = inv
        with get_db() as conn:
            save_user_game_state(conn, user_id, game_state)
        return {"ok": True, "game_state": game_state}

    if body.action == "unequip":
        if it.get("category") != "equipment":
            raise HTTPException(status_code=400, detail="非裝備")
        inv["items"][idx]["equipped"] = False
        game_state["inventory"] = inv
        with get_db() as conn:
            save_user_game_state(conn, user_id, game_state)
        return {"ok": True, "game_state": game_state}

    if body.action != "use":
        raise HTTPException(status_code=400, detail="不支援的動作")

    if it.get("category") not in ("consumable", "material"):
        raise HTTPException(status_code=400, detail="僅補給或物品可消耗使用")
    if int(it.get("quantity") or 0) <= 0:
        raise HTTPException(status_code=400, detail="數量不足")

    inv["items"][idx]["quantity"] = int(inv["items"][idx]["quantity"]) - 1
    if inv["items"][idx]["quantity"] <= 0:
        inv["items"].pop(idx)
    game_state["inventory"] = inv

    # 與前端確認文案對齊：玩家行動核心為「使用「物品名」」供 Poe 結算
    choice_line = f"【道號｜{player_name}】使用「{it['name']}」"
    user_content = f"【玩家行動】{choice_line}。請結算因果、代價與餘量。"

    msgs: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt_with_session_context(game_state)},
    ]
    for m in game_state["messages"]:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": user_content})

    raw = await _call_poe(msgs)

    prev_stats = dict(game_state["stats"])
    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        parsed = {"narrative": raw}

    narrative = parsed.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        narrative = raw

    narrative_raw = str(narrative).strip()
    clean_nar, stats = _merge_narrative_attrs_into_stats(
        narrative_raw, parsed, prev_stats
    )
    raw_stored = _rewrite_assistant_raw_with_clean_narrative(raw, clean_nar)

    game_state["messages"].append({"role": "user", "content": user_content})
    game_state["messages"].append({"role": "assistant", "content": raw_stored})
    if len(game_state["messages"]) > 48:
        game_state["messages"] = game_state["messages"][-48:]
    game_state["stats"] = stats
    game_state["last_narrative"] = clean_nar
    _merge_optional_collections(game_state, parsed)
    game_state["inventory"] = parse_loot_from_narrative(
        clean_nar, game_state["inventory"]
    )
    game_state["skills"] = parse_skill_learn_from_narrative(
        clean_nar, game_state["skills"]
    )
    apply_derived_rank_from_skills(game_state)
    apply_energy_max_from_skill_weights(game_state)
    clamp_and_sync_energy(game_state)

    with get_db() as conn:
        save_user_game_state(conn, user_id, game_state)

    return {
        "narrative": clean_nar,
        "stats": stats,
        "assistant_raw": raw_stored,
        "game_state": game_state,
    }


@app.post("/api/skill_action")
async def skill_action(body: SkillActionBody, auth: AuthUser) -> dict[str, Any]:
    user_id, _username, player_name, game_state = auth
    _ensure_game_state_shape(game_state)
    sk = normalize_skills(game_state["skills"], None)
    game_state["skills"] = sk
    sid = body.skill_id.strip()
    idx = _find_skill_index(sk["entries"], sid)
    if idx < 0:
        raise HTTPException(status_code=404, detail="找不到該武學")
    ent = sk["entries"][idx]

    if body.action == "abandon":
        sk["entries"].pop(idx)
        game_state["skills"] = normalize_skills(sk, None)
        apply_derived_rank_from_skills(game_state)
        apply_energy_max_from_skill_weights(game_state)
        clamp_and_sync_energy(game_state)
        with get_db() as conn:
            save_user_game_state(conn, user_id, game_state)
        return {"ok": True, "game_state": game_state}

    if body.action != "cultivate":
        raise HTTPException(status_code=400, detail="不支援的動作")

    choice_line = (
        f"【道號｜{player_name}】運功修煉「{ent['name']}」，消耗因果，"
        f"請結算境界與武學變化。"
    )
    user_content = f"【玩家行動】{choice_line}"

    msgs: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt_with_session_context(game_state)},
    ]
    for m in game_state["messages"]:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": user_content})

    raw = await _call_poe(msgs)

    prev_stats = dict(game_state["stats"])
    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        parsed = {"narrative": raw}

    narrative = parsed.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        narrative = raw

    narrative_raw = str(narrative).strip()
    clean_nar, stats = _merge_narrative_attrs_into_stats(
        narrative_raw, parsed, prev_stats
    )
    raw_stored = _rewrite_assistant_raw_with_clean_narrative(raw, clean_nar)

    game_state["messages"].append({"role": "user", "content": user_content})
    game_state["messages"].append({"role": "assistant", "content": raw_stored})
    if len(game_state["messages"]) > 48:
        game_state["messages"] = game_state["messages"][-48:]
    game_state["stats"] = stats
    game_state["last_narrative"] = clean_nar
    _merge_optional_collections(game_state, parsed)
    game_state["inventory"] = parse_loot_from_narrative(
        clean_nar, game_state["inventory"]
    )
    game_state["skills"] = parse_skill_learn_from_narrative(
        clean_nar, game_state["skills"]
    )
    apply_derived_rank_from_skills(game_state)
    apply_energy_max_from_skill_weights(game_state)
    clamp_and_sync_energy(game_state)

    with get_db() as conn:
        save_user_game_state(conn, user_id, game_state)

    return {
        "narrative": clean_nar,
        "stats": stats,
        "assistant_raw": raw_stored,
        "game_state": game_state,
    }


@app.post("/api/realm_shuttle")
async def realm_shuttle(body: RealmShuttleBody, auth: AuthUser) -> dict[str, Any]:
    """界域穿梭：更新 current_location 並請模型生成新位面開場與主線鉤子；不清空訊息與面板。"""
    user_id, _username, player_name, game_state = auth
    _ensure_game_state_shape(game_state)
    rid = normalize_realm_shuttle_id(body.realm)
    if not rid:
        raise HTTPException(status_code=400, detail="未知的界域")
    preset = REALM_SHUTTLE_PRESETS[rid]
    shuttle_address = (
        random_mundane_fictional_address() if rid == "mundane" else preset["address"]
    )
    game_state["current_location"] = normalize_current_location(
        {
            "plane": preset["plane"],
            "world": preset["world"],
            "address": shuttle_address,
        }
    )
    plane = game_state["current_location"]["plane"]
    world = game_state["current_location"]["world"]
    address = game_state["current_location"]["address"]
    sys_evt = (
        "【界域穿梭｜系統事件】\n"
        f"玩家已透過因果界門錨定至：位面「{plane}」／世界「{world}」／落點「{address}」。"
        "行囊、武學、同伴與完整對話記憶均已保留；請寫一段銜接舊因果的短開場，並拋出一條清晰主線任務鉤子。"
        f"{preset['prompt_hint']}"
        "嚴禁現實地名；JSON 必含 current_location（可与上述一致或僅微調 address）；仍須遵守【因果結算】【現狀】【抉擇】與 [ATTR]。"
    )
    choice_line = f"【道號｜{player_name}】啟動界域穿梭，於新錨點清醒／落地。"
    user_content = f"{sys_evt}\n\n【玩家行動】{choice_line}"

    msgs: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt_with_session_context(game_state)},
    ]
    for m in game_state["messages"]:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": user_content})

    raw = await _call_poe(msgs, temperature=0.86)

    prev_stats = dict(game_state["stats"])
    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        parsed = {"narrative": raw}

    narrative = parsed.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        narrative = raw

    narrative_raw = str(narrative).strip()
    clean_nar, stats = _merge_narrative_attrs_into_stats(
        narrative_raw, parsed, prev_stats
    )
    raw_stored = _rewrite_assistant_raw_with_clean_narrative(raw, clean_nar)

    game_state["messages"].append({"role": "user", "content": user_content})
    game_state["messages"].append({"role": "assistant", "content": raw_stored})
    if len(game_state["messages"]) > 48:
        game_state["messages"] = game_state["messages"][-48:]
    game_state["stats"] = stats
    game_state["last_narrative"] = clean_nar
    _merge_optional_collections(game_state, parsed)
    game_state["inventory"] = parse_loot_from_narrative(
        clean_nar, game_state["inventory"]
    )
    game_state["skills"] = parse_skill_learn_from_narrative(
        clean_nar, game_state["skills"]
    )
    apply_derived_rank_from_skills(game_state)
    apply_energy_max_from_skill_weights(game_state)
    clamp_and_sync_energy(game_state)

    bump_quest_action_tick(game_state)
    await maybe_evolve_shelved_quests(game_state, force=True)

    with get_db() as conn:
        save_user_game_state(conn, user_id, game_state)

    return {
        "narrative": clean_nar,
        "stats": stats,
        "assistant_raw": raw_stored,
        "game_state": game_state,
    }


@app.post("/api/quests_open")
async def quests_open(auth: AuthUser) -> dict[str, Any]:
    """讀取並正規化任務冊（寫回存檔）；前端開啟任務彈窗時呼叫。"""
    user_id, _username, _player_name, game_state = auth
    _ensure_game_state_shape(game_state)
    with get_db() as conn:
        save_user_game_state(conn, user_id, game_state)
    return {"quests": game_state["quests"], "game_state": game_state}


@app.post("/api/quests_ack_causal")
async def quests_ack_causal(auth: AuthUser) -> dict[str, Any]:
    """關閉名冊後清除 [因果已變] 標記。"""
    user_id, _username, _player_name, game_state = auth
    _ensure_game_state_shape(game_state)
    q = normalize_quests_model(game_state.get("quests"))
    for e in q["entries"]:
        e["causal_shift"] = False
    game_state["quests"] = q
    with get_db() as conn:
        save_user_game_state(conn, user_id, game_state)
    return {"ok": True, "game_state": game_state}


@app.post("/api/quest_track")
async def quest_track(body: QuestTrackBody, auth: AuthUser) -> dict[str, Any]:
    user_id, _username, _player_name, game_state = auth
    _ensure_game_state_shape(game_state)
    q = normalize_quests_model(game_state.get("quests"))
    qid = body.quest_id.strip()
    found = False
    for e in q["entries"]:
        if e.get("id") != qid:
            continue
        found = True
        if body.action == "shelve":
            e["status"] = "shelved"
            e["shelved_at_tick"] = int(q.get("player_action_tick") or 0)
        else:
            e["status"] = "tracking"
            e["shelved_at_tick"] = None
        break
    if not found:
        raise HTTPException(status_code=404, detail="找不到該條因果")
    game_state["quests"] = q
    with get_db() as conn:
        save_user_game_state(conn, user_id, game_state)
    return {"ok": True, "game_state": game_state}


@app.post("/api/turn")
async def turn(body: TurnBody, auth: AuthUser) -> dict[str, Any]:
    user_id, game_state, msgs, user_content, choice_stripped = (
        await _turn_prepare_messages(body, auth)
    )
    raw = await _call_poe(msgs)
    return await _finalize_turn_from_model_raw(
        user_id, raw, game_state, user_content, choice_stripped
    )


def _sse_error_payload(exc: HTTPException) -> str:
    d = exc.detail
    if isinstance(d, list):
        d = " ".join(
            str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d
        )
    return json.dumps({"error": str(d)}, ensure_ascii=False)


@app.post("/api/turn_stream")
async def turn_stream(body: TurnBody, auth: AuthUser) -> StreamingResponse:
    user_id, game_state, msgs, user_content, choice_stripped = (
        await _turn_prepare_messages(body, auth)
    )

    async def event_gen():
        acc: list[str] = []
        last_preview = ""
        try:
            async for piece in _call_poe_stream(msgs):
                acc.append(piece)
                full = "".join(acc)
                preview = _partial_narrative_for_stream_preview(full)
                if preview.startswith(last_preview):
                    delta = preview[len(last_preview) :]
                else:
                    delta = preview
                last_preview = preview
                if delta:
                    yield (
                        "data: "
                        + json.dumps(
                            {"narrative_delta": delta}, ensure_ascii=False
                        )
                        + "\n\n"
                    )
            raw = "".join(acc).strip()
            if not raw:
                yield "data: " + json.dumps({"error": "模型未回傳內容"}) + "\n\n"
                return
            result = await _finalize_turn_from_model_raw(
                user_id, raw, game_state, user_content, choice_stripped
            )
            result["done"] = True
            yield "data: " + json.dumps(result, ensure_ascii=False) + "\n\n"
        except HTTPException as e:
            yield "data: " + _sse_error_payload(e) + "\n\n"
        except Exception as e:
            yield (
                "data: "
                + json.dumps({"error": str(e)}, ensure_ascii=False)
                + "\n\n"
            )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    # 本機未設定 PORT 時預設 8000；Render / Zeabur 等會注入 PORT，必須依該值綁定。
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("UVICORN_RELOAD", "1").strip().lower() in ("1", "true", "yes"),
    )
