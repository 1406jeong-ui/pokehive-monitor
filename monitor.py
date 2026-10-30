"""
The Poke Hive (Shopify) 신상품 / 재입고 알림 스크립트

동작:
  1) https://www.thepokehive.com/products.json 에서 전체 상품을 가져옴
  2) 지난 실행 때 저장한 state.json 과 비교
     - 처음 보는 상품  -> "신상품" 알림
     - 품절(available=false) -> 재고(true) 로 바뀐 옵션 -> "재입고" 알림
  3) 알림은 ntfy(폰 푸시) 와/또는 Discord 웹훅으로 전송

환경변수:
  NTFY_TOPIC        ntfy 토픽 이름 (예: pokehive-xyz123, 남이 못 맞히게 길게)
  DISCORD_WEBHOOK   (선택) 디스코드 채널 웹훅 URL
  KEYWORDS          (선택) 쉼표 구분 키워드. 지정하면 제목에 포함된 상품만 알림
                    예: "Elite Trainer,Booster Bundle,Prismatic"

필요 패키지: pip install requests
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

STORE = "https://www.thepokehive.com"
STATE_FILE = Path(__file__).with_name("state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (personal restock monitor)"}

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]


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
    """{product_id: {title, handle, variants: {variant_id: {title, available, price}}}}"""
    snap = {}
    for p in products:
        snap[str(p["id"])] = {
            "title": p["title"],
            "handle": p["handle"],
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


def matches_keywords(title):
    return not KEYWORDS or any(k in title.lower() for k in KEYWORDS)


def diff(old, new):
    events = []
    for pid, p in new.items():
        url = f"{STORE}/products/{p['handle']}"
        if not matches_keywords(p["title"]):
            continue
        if pid not in old:
            in_stock = any(v["available"] for v in p["variants"].values())
            price = next(iter(p["variants"].values()), {}).get("price")
            events.append(("🆕 신상품", f"{p['title']} (${price}) {'재고있음' if in_stock else '품절/예정'}", url))
            continue
        for vid, v in p["variants"].items():
            was = old[pid]["variants"].get(vid, {}).get("available", False)
            if v["available"] and not was:
                name = p["title"] if v["title"] in ("", "Default Title") else f"{p['title']} - {v['title']}"
                events.append(("🔔 재입고", f"{name} (${v['price']})", url))
    return events


def notify(tag, msg, url):
    print(f"[{tag}] {msg} {url}")
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": f"PokeHive {tag}".encode("utf-8"), "Click": url, "Priority": "high"},
            timeout=15,
        )
    if DISCORD_WEBHOOK:
        requests.post(DISCORD_WEBHOOK, json={"content": f"**{tag}** {msg}\n{url}"}, timeout=15)


def main():
    new = snapshot(fetch_all_products())
    if not STATE_FILE.exists():
        # 첫 실행: 기준선만 저장하고 알림은 보내지 않음 (수백 개 알림 폭탄 방지)
        STATE_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=1))
        print(f"기준선 저장 완료: 상품 {len(new)}개")
        return
    old = json.loads(STATE_FILE.read_text())
    events = diff(old, new)
    for e in events[:20]:  # 한 번에 너무 많이 오지 않게 제한
        notify(*e)
    if len(events) > 20:
        notify("ℹ️ 기타", f"외 {len(events) - 20}건 변경", STORE + "/collections/all")
    STATE_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=1))
    print(f"확인 완료: 상품 {len(new)}개, 알림 {len(events)}건")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print("오류:", ex, file=sys.stderr)
        sys.exit(1)
