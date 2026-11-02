"""
The Poke Hive (Shopify) 신상품 / 재입고 알림 스크립트

동작:
  1) https://www.thepokehive.com/products.json 에서 전체 상품을 가져옴
  2) 지난 실행 때 저장한 state.json 과 비교
     - 처음 보는 상품  -> "신상품" 알림
     - 품절(available=false) -> 재고(true) 로 바뀐 옵션 -> "재입고" 알림
  3) 디스코드에는 카드(임베드) 형태로 전송:
     제품명(클릭 시 상품 페이지), 사진, 가격, 재고, 바로구매/장바구니 링크

환경변수:
  DISCORD_WEBHOOK   디스코드 채널 웹훅 URL
  NTFY_TOPIC        (선택) ntfy 폰 푸시 토픽
  KEYWORDS          (선택) 쉼표 구분 키워드. 지정하면 제목에 포함된 상품만 알림
                    예: "Elite Trainer,Booster Bundle,Prismatic"
  BUY_QTYS          (선택) 바로구매 링크 수량 목록. 기본 "1,2,5,6"

필요 패키지: pip install requests
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

STORE = "https://www.thepokehive.com"
STORE_NAME = "ThePokeHive"
STATE_FILE = Path(__file__).with_name("state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (personal restock monitor)"}
KST = timezone(timedelta(hours=9))

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
BUY_QTYS = [int(q) for q in (os.getenv("BUY_QTYS") or "1,2,5,6").split(",") if q.strip().isdigit()]

MAX_VARIANTS_SHOWN = 5   # 옵션이 많은 상품은 앞의 5개만 링크 표시
MAX_EVENTS = 30          # 한 번 실행에 보낼 최대 알림 수


# ---------------------------------------------------------------- 데이터 수집

def fetch_all_products():
    products, page = [], 1
    while True:
        r = requests.get(
            f"{STORE}/products.json",
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


def snapshot(products):
    snap = {}
    for p in products:
        images = p.get("images") or []
        snap[str(p["id"])] = {
            "title": p["title"],
            "handle": p["handle"],
            "image": images[0]["src"] if images else None,
            "variants": {
                str(v["id"]): {
                    "title": v.get("title", ""),
                    "available": bool(v.get("available")),
                    "price": v.get("price"),
                }
                for v in p.get("variants", [])
            },
        }
    return snap


# ---------------------------------------------------------------- 비교

def fmt_price(p):
    """이 사이트는 가격을 센트 단위 문자열("61000")로 주므로 달러로 변환"""
    if p is None:
        return "?"
    s = str(p)
    if "." in s:
        return s
    try:
        return f"{int(s) / 100:,.2f}"
    except ValueError:
        return s


def matches_keywords(title):
    return not KEYWORDS or any(k in title.lower() for k in KEYWORDS)


def diff(old, new):
    """이벤트 목록: {"kind": "new"|"restock", "product": {...}, "variant_ids": [...]}"""
    events = []
    for pid, p in new.items():
        if not matches_keywords(p["title"]):
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
    return events


# ---------------------------------------------------------------- 링크

def checkout_link(vid, qty):
    """누르면 해당 수량이 담긴 상태로 바로 결제 화면으로 이동"""
    return f"{STORE}/cart/{vid}:{qty}"


def add_to_cart_link(vid, qty=1):
    """누르면 장바구니에 담고 장바구니 화면으로 이동 (기존 장바구니 유지)"""
    return f"{STORE}/cart/add?id={vid}&quantity={qty}"


def variant_label(v):
    return "기본" if v["title"] in ("", "Default Title") else v["title"]


# ---------------------------------------------------------------- 디스코드 카드

def build_embed(ev):
    p = ev["product"]
    url = f"{STORE}/products/{p['handle']}"
    vids = ev["variant_ids"][:MAX_VARIANTS_SHOWN]
    variants = p["variants"]
    first = variants[vids[0]] if vids else next(iter(variants.values()), {})
    in_stock = any(variants[v]["available"] for v in vids)

    buy_lines, cart_lines = [], []
    for vid in vids:
        v = variants[vid]
        label = variant_label(v)
        if v["available"]:
            qty_links = " | ".join(f"[{q}개]({checkout_link(vid, q)})" for q in BUY_QTYS)
            buy_lines.append(f"{label}: {qty_links}")
            cart_lines.append(f"[{label} 담기]({add_to_cart_link(vid)})")
        else:
            buy_lines.append(f"{label}: 품절/예약 대기")
    if len(ev["variant_ids"]) > MAX_VARIANTS_SHOWN:
        buy_lines.append(f"외 옵션 {len(ev['variant_ids']) - MAX_VARIANTS_SHOWN}개는 상품 페이지에서 확인")

    is_new = ev["kind"] == "new"
    embed = {
        "author": {"name": f"{'🆕 신상품' if is_new else '🔔 재입고'} | {STORE_NAME}", "url": STORE},
        "title": p["title"][:256],
        "url": url,
        "color": 0x3498DB if is_new else 0x2ECC71,
        "fields": [
            {"name": "가격", "value": f"${fmt_price(first.get('price'))} USD", "inline": True},
            {"name": "재고", "value": "재고 있음 ✅" if in_stock else "품절 / 예약 전 ⏳", "inline": True},
            {"name": "바로 결제 (수량 선택)", "value": "\n".join(buy_lines)[:1024] or "-", "inline": False},
        ],
        "footer": {"text": f"PokeHive Monitor | {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST"},
    }
    if cart_lines:
        embed["fields"].append({"name": "장바구니에 담기", "value": " | ".join(cart_lines)[:1024], "inline": False})
    embed["fields"].append({"name": "링크", "value": f"[상품 페이지]({url}) | [장바구니]({STORE}/cart) | [전체 상품]({STORE}/collections/all)", "inline": False})
    if p.get("image"):
        embed["thumbnail"] = {"url": p["image"]}
    return embed


def send_discord(embeds):
    # 디스코드는 메시지 1개에 카드 최대 10개
    for i in range(0, len(embeds), 10):
        for attempt in range(3):
            r = requests.post(
                DISCORD_WEBHOOK,
                json={"username": "PokeHive 알림", "embeds": embeds[i:i + 10]},
                timeout=15,
            )
            if r.status_code == 429:  # 너무 빠르면 잠깐 대기 후 재시도
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            r.raise_for_status()
            break
        time.sleep(1)


def send_ntfy(ev):
    p = ev["product"]
    tag = "신상품" if ev["kind"] == "new" else "재입고"
    first = p["variants"][ev["variant_ids"][0]] if ev["variant_ids"] else {}
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=f"{p['title']} (${fmt_price(first.get('price'))})".encode("utf-8"),
        headers={"Title": f"PokeHive {tag}".encode("utf-8"),
                 "Click": f"{STORE}/products/{p['handle']}", "Priority": "high"},
        timeout=15,
    )


# ---------------------------------------------------------------- 실행

def main():
    new = snapshot(fetch_all_products())
    if not STATE_FILE.exists():
        # 첫 실행: 기준선만 저장하고 알림은 보내지 않음 (수백 개 알림 폭탄 방지)
        STATE_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=1))
        print(f"기준선 저장 완료: 상품 {len(new)}개")
        return
    old = json.loads(STATE_FILE.read_text())
    events = diff(old, new)[:MAX_EVENTS]
    for ev in events:
        print(f"[{ev['kind']}] {ev['product']['title']}")
    if events and DISCORD_WEBHOOK:
        send_discord([build_embed(ev) for ev in events])
    if events and NTFY_TOPIC:
        for ev in events:
            send_ntfy(ev)
    STATE_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=1))
    print(f"확인 완료: 상품 {len(new)}개, 알림 {len(events)}건")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print("오류:", ex, file=sys.stderr)
        sys.exit(1)
