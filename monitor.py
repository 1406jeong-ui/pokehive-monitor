"""
Shopify 쇼핑몰 신상품 / 재입고 디스코드 알림 스크립트

감시 대상 (아래 STORES 에서 수정 가능):
  - The Poke Hive      : 신상품 + 재입고, 전체 상품
  - Kollect Korner     : 신상품만, 제목/분류/태그에 Pokemon 또는 One Piece 가 들어간 상품
  - Collector Store    : 신상품만, 제목/분류/태그에 Pokemon 또는 One Piece 가 들어간 상품

동작:
  1) 각 쇼핑몰의 /products.json 을 가져옴 (최신 등록 순)
  2) 쇼핑몰별 상태 파일(state*.json)과 비교해 새 상품 / 재입고를 찾음
  3) 디스코드에 이미지 카드(임베드)로 전송:
     제품명(클릭 시 상품 페이지), 사진, 가격, 재고, 바로결제/장바구니 링크
  첫 실행(상태 파일이 없을 때)은 기준선만 저장하고 알림은 보내지 않음

환경변수:
  DISCORD_WEBHOOK   디스코드 채널 웹훅 URL
  NTFY_TOPIC        (선택) ntfy 폰 푸시 토픽
  KEYWORDS          (선택) The Poke Hive 전용 키워드 필터 (쉼표 구분)
  BUY_QTYS          (선택) 바로결제 링크 수량 목록. 기본 "1,2,5,6"

필요 패키지: pip install requests
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).parent
HEADERS = {"User-Agent": "Mozilla/5.0 (personal restock monitor)"}
KST = timezone(timedelta(hours=9))

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
POKEHIVE_KEYWORDS = [k.strip() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
BUY_QTYS = [int(q) for q in (os.getenv("BUY_QTYS") or "1,2,5,6").split(",") if q.strip().isdigit()]

# 등급 카드 제목 패턴 (PSA·CGC·BGS·TAG 등 그레이딩 회사 + 점수)
GRADED_REGEX = (
    r"\b(PSA|BGS|BVG|CGC|SGC|TAG|PCG|ACE|GMA|HGA|AGS|CSG|ARS|MNT)\b"
    r"(\s+(GEM\s*(MT|MINT)|MINT|NM-MT|PRISTINE|AUTH(ENTIC)?))?\s*\d{1,2}(\.\d)?\b"
)

# ---------------------------------------------------------------- 감시할 쇼핑몰 목록
#  mode      "all" = 신상품 + 재입고 / "new" = 신상품만
#  keywords  비어 있으면 모든 상품, 있으면 하나라도 포함된 상품만 알림 (대소문자·악센트 무시)
#  pages     "new" 모드에서 확인할 최신 상품 페이지 수 (1페이지 = 250개)
STORES = [
    {
        "name": "ThePokeHive",
        "url": "https://www.thepokehive.com",
        "state": "state.json",
        "mode": "all",
        "keywords": POKEHIVE_KEYWORDS,
        "color_new": 0x3498DB,
    },
    {
        "name": "Kollect Korner",
        "url": "https://www.kollectkorner.com",
        "state": "state_kollectkorner.json",
        "mode": "new",
        "pages": 2,
        "keywords": ["pokemon", "one piece"],
        "color_new": 0x9B59B6,
    },
    {
        "name": "Collector Store",
        "url": "https://collectorstore.com",
        "state": "state_collectorstore.json",
        "mode": "new",
        "pages": 2,
        "keywords": ["pokemon", "one piece"],
        # 등급 카드(슬랩) 제외: 제목에 "PSA 10", "CGC 9.5", "BGS 9.5", "TAG 10" 같은 표기가 있으면 알림 안 함
        "exclude_regex": GRADED_REGEX,
        "color_new": 0xE67E22,
    },
]

MAX_VARIANTS_SHOWN = 5    # 옵션이 많은 상품은 앞의 5개만 링크 표시
MAX_EVENTS = 30           # 쇼핑몰별 한 번에 보낼 최대 알림 수
SEEN_LIMIT = 5000         # "new" 모드에서 기억할 상품 ID 수


# ---------------------------------------------------------------- 공통 함수

def norm(s):
    """대소문자·악센트 무시 비교용 (Pokémon -> pokemon)"""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def fetch_products(store, max_pages=None):
    products, page = [], 1
    while max_pages is None or page <= max_pages:
        r = requests.get(
            f"{store['url']}/products.json",
            params={"limit": 250, "page": page},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
        time.sleep(1)  # 서버에 부담 주지 않도록
    return products


def simplify(p):
    images = p.get("images") or []
    return {
        "title": p["title"],
        "handle": p["handle"],
        "image": images[0]["src"] if images else None,
        "match_text": norm(" ".join([p["title"], p.get("product_type", ""), " ".join(p.get("tags", []))])),
        "variants": {
            str(v["id"]): {
                "title": v.get("title", ""),
                "available": bool(v.get("available")),
                "price": v.get("price"),
            }
            for v in p.get("variants", [])
        },
    }


def matches(store, product):
    ex = store.get("exclude_regex")
    if ex and re.search(ex, product["title"], re.IGNORECASE):
        return False
    kws = [norm(k) for k in store["keywords"]]
    return not kws or any(k in product["match_text"] for k in kws)


def fmt_price(p):
    """가격이 "149.99" 또는 센트 단위 "14999" 로 오는 경우 모두 달러로 표시"""
    if p is None:
        return "?"
    s = str(p)
    if "." in s:
        return s
    try:
        return f"{int(s) / 100:,.2f}"
    except ValueError:
        return s


def load_state(path):
    return json.loads(path.read_text()) if path.exists() else None


def save_state(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 쇼핑몰별 확인

def check_all_mode(store):
    """신상품 + 재입고 (전체 상품 비교)"""
    path = HERE / store["state"]
    new = {str(p["id"]): simplify(p) for p in fetch_products(store)}
    old = load_state(path)
    events = []
    if old is not None:
        for pid, p in new.items():
            if not matches(store, p):
                continue
            if pid not in old:
                events.append({"kind": "new", "product": p, "variant_ids": list(p["variants"])})
                continue
            restocked = [
                vid for vid, v in p["variants"].items()
                if v["available"] and not old[pid]["variants"].get(vid, {}).get("available", False)
            ]
            if restocked:
                events.append({"kind": "restock", "product": p, "variant_ids": restocked})
    save_state(path, {pid: {k: v for k, v in p.items() if k != "match_text"} for pid, p in new.items()})
    return events, old is None, len(new)


def check_new_mode(store):
    """신상품만 (최신 N페이지만 확인, 본 적 없는 상품 ID 를 새 상품으로 판단)"""
    path = HERE / store["state"]
    latest = [(str(p["id"]), simplify(p)) for p in fetch_products(store, store.get("pages", 2))]
    state = load_state(path)
    seen = state.get("seen", []) if state else []
    seen_set = set(seen)
    events = []
    if state is not None:
        for pid, p in latest:
            if pid not in seen_set and matches(store, p):
                events.append({"kind": "new", "product": p, "variant_ids": list(p["variants"])})
    new_ids = [pid for pid, _ in latest if pid not in seen_set]
    save_state(path, {"seen": (new_ids + seen)[:SEEN_LIMIT]})
    return events, state is None, len(latest)


# ---------------------------------------------------------------- 링크

def checkout_link(store, vid, qty):
    """누르면 해당 수량이 담긴 상태로 바로 결제 화면으로 이동"""
    return f"{store['url']}/cart/{vid}:{qty}"


def add_to_cart_link(store, vid, qty=1):
    """누르면 장바구니에 담고 장바구니 화면으로 이동 (기존 장바구니 유지)"""
    return f"{store['url']}/cart/add?id={vid}&quantity={qty}"


def variant_label(v):
    return "기본" if v["title"] in ("", "Default Title") else v["title"]


# ---------------------------------------------------------------- 디스코드 카드

def build_embed(store, ev):
    p = ev["product"]
    base = store["url"]
    url = f"{base}/products/{p['handle']}"
    vids = ev["variant_ids"][:MAX_VARIANTS_SHOWN]
    variants = p["variants"]
    first = variants[vids[0]] if vids else {}
    in_stock = any(variants[v]["available"] for v in vids)

    buy_lines, cart_lines = [], []
    for vid in vids:
        v = variants[vid]
        label = variant_label(v)
        if v["available"]:
            qty_links = " | ".join(f"[{q}개]({checkout_link(store, vid, q)})" for q in BUY_QTYS)
            buy_lines.append(f"{label}: {qty_links}")
            cart_lines.append(f"[{label} 담기]({add_to_cart_link(store, vid)})")
        else:
            buy_lines.append(f"{label}: 품절/예약 대기")
    if len(ev["variant_ids"]) > MAX_VARIANTS_SHOWN:
        buy_lines.append(f"외 옵션 {len(ev['variant_ids']) - MAX_VARIANTS_SHOWN}개는 상품 페이지에서 확인")

    is_new = ev["kind"] == "new"
    embed = {
        "author": {"name": f"{'🆕 신상품' if is_new else '🔔 재입고'} | {store['name']}", "url": base},
        "title": p["title"][:256],
        "url": url,
        "color": store.get("color_new", 0x3498DB) if is_new else 0x2ECC71,
        "fields": [
            {"name": "가격", "value": f"${fmt_price(first.get('price'))} USD", "inline": True},
            {"name": "재고", "value": "재고 있음 ✅" if in_stock else "품절 / 예약 전 ⏳", "inline": True},
            {"name": "바로 결제 (수량 선택)", "value": "\n".join(buy_lines)[:1024] or "-", "inline": False},
        ],
        "footer": {"text": f"{store['name']} Monitor | {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST"},
    }
    if cart_lines:
        embed["fields"].append({"name": "장바구니에 담기", "value": " | ".join(cart_lines)[:1024], "inline": False})
    embed["fields"].append({"name": "링크", "value": f"[상품 페이지]({url}) | [장바구니]({base}/cart) | [전체 상품]({base}/collections/all)", "inline": False})
    if p.get("image"):
        embed["image" if store["mode"] == "new" else "thumbnail"] = {"url": p["image"]}
    return embed


def send_discord(embeds):
    # 디스코드는 메시지 1개에 카드 최대 10개
    for i in range(0, len(embeds), 10):
        for _ in range(3):
            r = requests.post(
                DISCORD_WEBHOOK,
                json={"username": "카드 입고 알림", "embeds": embeds[i:i + 10]},
                timeout=15,
            )
            if r.status_code == 429:  # 너무 빠르면 잠깐 대기 후 재시도
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            r.raise_for_status()
            break
        time.sleep(1)


def send_ntfy(store, ev):
    p = ev["product"]
    tag = "신상품" if ev["kind"] == "new" else "재입고"
    first = p["variants"][ev["variant_ids"][0]] if ev["variant_ids"] else {}
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=f"{p['title']} (${fmt_price(first.get('price'))})".encode("utf-8"),
        headers={"Title": f"{store['name']} {tag}".encode("utf-8"),
                 "Click": f"{store['url']}/products/{p['handle']}", "Priority": "high"},
        timeout=15,
    )


# ---------------------------------------------------------------- 실행

def main():
    failed = 0
    for store in STORES:
        try:
            checker = check_all_mode if store["mode"] == "all" else check_new_mode
            events, first_run, count = checker(store)
            if first_run:
                print(f"[{store['name']}] 기준선 저장 완료: 상품 {count}개")
                continue
            events = events[:MAX_EVENTS]
            for ev in events:
                print(f"[{store['name']}] [{ev['kind']}] {ev['product']['title']}")
            if events and DISCORD_WEBHOOK:
                send_discord([build_embed(store, ev) for ev in events])
            if events and NTFY_TOPIC:
                for ev in events:
                    send_ntfy(store, ev)
            print(f"[{store['name']}] 확인 완료: 상품 {count}개, 알림 {len(events)}건")
        except Exception as ex:  # 한 쇼핑몰이 실패해도 나머지는 계속 확인
            failed += 1
            print(f"[{store['name']}] 오류: {ex}", file=sys.stderr)
    if failed == len(STORES):
        sys.exit(1)


if __name__ == "__main__":
    main()
