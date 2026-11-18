#!/usr/bin/env python3
"""Fetch a stock photo for every menu item from Wikimedia Commons and cache it locally.

  python3 frontend/scripts/fetch_menu_images.py            # only items that have no cached photo yet
  python3 frontend/scripts/fetch_menu_images.py --force    # re-fetch everything
  python3 frontend/scripts/fetch_menu_images.py --only "Masala Dosa"

Photos are downloaded ONCE into frontend/public/menu-images/<slug>.webp (center-cropped to 16:10, ~20 KB each) and
served locally – nothing is hot-linked, so the dashboard keeps working offline. Only CC0 / public-domain / CC BY /
CC BY-SA files are accepted; author + license + source page are written to menu-images/CREDITS.json (attribution).
Then menu.json's `image` field is pointed at the cached file.
"""
import argparse, io, json, re, sys, time
from pathlib import Path
import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
MENU = ROOT / "src/argo_mini/menu/menu.json"
OUT = ROOT / "frontend/public/menu-images"
CREDITS = OUT / "CREDITS.json"
API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "ArgoSonicMenuImages/1.0 (restaurant robot dashboard; contact: services@atsnai.com)"}
W, H = 480, 300                                     # stored size (16:10); the UI scales it down
OK_LICENSE = re.compile(r"^(cc0|public domain|pd|cc[- ]by([- ]sa)?\b)", re.I)
BAD_TITLE = re.compile(r"\b(map|flag|logo|diagram|menu|stamp|drawing|painting|illustration|poster|advert|packag|restaurant|shop|"
                       r"stall|vendor|seller|chart|icon|recipe card|label|bottle|can\b|machine|factory|tomb|temple|statue)\b", re.I)

# search phrase per dish where the plain name is a poor query
QUERY = {
 "Tandoori Chicken (Half)": "Tandoori chicken", "Chicken 65": "Chicken 65 dish", "Hara Bhara Kebab": "Hara bhara kabab",
 "Veg Seekh Kebab": "Seekh kebab", "Amritsari Fish Fry": "Amritsari fish", "Samosa (2 pcs)": "Samosa",
 "Idli Sambar (3 pcs)": "Idli sambar", "Medu Vada (2 pcs)": "Medu vada", "Onion Uttapam": "Uttapam", "Ghee Podi Dosa": "Dosa podi",
 "Goan Fish Curry": "Goan fish curry", "Veg Dum Biryani": "Vegetable biryani", "Hyderabadi Chicken Dum Biryani": "Hyderabadi chicken biryani",
 "Mutton Dum Biryani": "Mutton biryani", "Jeera Rice": "Jeera rice", "Steamed Rice": "Steamed basmati rice bowl", "Curd Rice": "Curd rice",
 "Butter Naan": "Butter naan", "Garlic Naan": "Garlic naan", "Tandoori Roti": "Tandoori roti", "Laccha Paratha": "Lachha paratha",
 "Amritsari Kulcha": "Amritsari kulcha", "Veg Thali": "Indian vegetarian thali", "Non-Veg Thali": "Non-vegetarian Indian thali",
 "Gulab Jamun (2 pcs)": "Gulab jamun", "Rasmalai (2 pcs)": "Rasmalai", "Gajar Halwa": "Gajar ka halwa", "Double Ka Meetha": "Double ka meetha",
 "Masala Chai": "Masala chai", "Filter Coffee": "South Indian filter coffee", "Fresh Lime Soda": "Nimbu soda lemonade", "Chaas (Spiced Buttermilk)": "Chaas buttermilk",
 "Cold Coffee": "Iced coffee glass", "Mutton Rogan Josh": "Rogan josh", "Chicken Chettinad": "Chicken Chettinad", "Egg Biryani": "Egg biryani",
}

# photos reviewed by eye and rejected (wrong dish / poor quality) – never picked again
REJECT = {"File:Indian Chicken Seekh Kebab.jpg", "File:Onion \"Uttapam\".jpg", "File:Butter chicken rice bowl - chaiiwala 2024-02-10.jpg",
          "File:Steamed rice in banana leaf.jpg", "File:Thali and mango lassi (30930939888).jpg", "File:Prepared African Salad Meal.jpg",
          "File:Jeera-rice.JPG", "File:Egg curry pic.jpg"}
QUERY.update({"Veg Seekh Kebab": "Vegetable kebab Indian", "Onion Uttapam": "Uttapam South Indian", "Butter Chicken": "Butter chicken murgh makhani",
              "Egg Curry": "Egg curry anda masala", "Steamed Rice": "Bowl of cooked white rice", "Mango Lassi": "Mango lassi glass",
              "Chaas (Spiced Buttermilk)": "Chaas Indian buttermilk glass", "Jaljeera": "Jaljeera drink", "Jeera Rice": "Jeera rice bowl"})

# exact Commons files chosen by hand where search can't find the dish under its menu name
FORCE = {"Chaas (Spiced Buttermilk)": "File:Masala Buttermilk.JPG", "Jaljeera": "File:Jal Zeera.jpg"}

def by_title(cl, title):
    r = cl.get(API, params={"action": "query", "format": "json", "titles": title, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 640}, timeout=30)
    r.raise_for_status()
    return list(r.json()["query"]["pages"].values())

def slug(n): return re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")

def strip(html): return re.sub(r"<[^>]+>", "", html or "").strip()

def search(cl, q):
    r = cl.get(API, params={"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrsearch": q + " filetype:bitmap",
                            "gsrlimit": 15, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 640}, timeout=30)
    r.raise_for_status()
    pages = sorted((r.json().get("query", {}).get("pages", {})).values(), key=lambda p: p.get("index", 99))
    return pages

def pick(pages, name, used):
    toks = [t for t in re.findall(r"[a-z]+", re.sub(r"\(.*?\)", "", name).lower()) if len(t) > 2 and t not in ("veg", "dum", "the")]
    best = None
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]; md = ii.get("extmetadata", {})
        lic = strip(md.get("LicenseShortName", {}).get("value"))
        title = p["title"]
        if title in REJECT: continue
        if ii.get("mime") not in ("image/jpeg", "image/png") or not OK_LICENSE.search(lic) or BAD_TITLE.search(title): continue
        if ii.get("width", 0) < 640 or ii.get("height", 0) < 420 or ii.get("thumburl") in used: continue
        ratio = ii["width"] / ii["height"]
        if ratio < 0.8 or ratio > 2.4: continue
        score = sum(t in title.lower() for t in toks) / max(1, len(toks)) - 0.02 * p.get("index", 0)
        if best is None or score > best[0]: best = (score, p, ii, md, lic)
    return best

def write_credits_html(credits):
    import html
    rows = "".join(f"<tr><td>{html.escape(n)}</td><td><a href=\"{html.escape(c['source'] or '')}\">{html.escape(c['title'][5:])}</a></td>"
                   f"<td>{html.escape(c['author'])}</td><td>{html.escape(c['license'])}</td></tr>" for n, c in credits.items())
    (OUT / "credits.html").write_text(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Menu photo credits</title><style>body{{font:14px/1.5 system-ui;margin:24px;color:#2a2017;background:#f6f2ea}}table{{border-collapse:collapse;background:#fff;width:100%}}
td,th{{border:1px solid #e3dac8;padding:7px 10px;text-align:left;vertical-align:top}}th{{background:#f4e6c4}}a{{color:#9a6c14}}</style>
<h1>Menu photo credits</h1><p>Photographs from <a href="https://commons.wikimedia.org">Wikimedia Commons</a>, used under the licenses shown
(CC0, public domain, CC BY, CC BY-SA). Images were resized and cropped for display.</p>
<table><tr><th>Dish</th><th>Photo (source page)</th><th>Author</th><th>License</th></tr>{rows}</table>""", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--force", action="store_true"); ap.add_argument("--only")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    doc = json.loads(MENU.read_text(encoding="utf-8"))
    credits = json.loads(CREDITS.read_text()) if CREDITS.exists() else {}
    used = {c.get("thumb") for c in credits.values()}
    missing = []
    with httpx.Client(headers=UA, follow_redirects=True) as cl:
        for it in doc["menu"]:
            n = it["name"]
            if a.only and n != a.only: continue
            f = OUT / f"{slug(n)}.webp"
            if f.exists() and not a.force:
                it["image"] = f"/menu-images/{f.name}"; continue
            best = None
            if n in FORCE:
                try: best = pick(by_title(cl, FORCE[n]), n, set()) or None
                except Exception as e: print("  by-title error", n, e)
                if best: best = (1.0, *best[1:])
            for q in ([] if best else dict.fromkeys([QUERY.get(n, re.sub(r"\(.*?\)", "", n).strip()), re.sub(r"\(.*?\)", "", n).strip() + " food"])):
                try: best = pick(search(cl, q), n, used)
                except Exception as e: print("  search error", n, e)
                time.sleep(0.4)
                if best and best[0] >= 0.5: break
            if not best: print("MISSING", n); missing.append(n); continue
            score, p, ii, md, lic = best
            try:
                img = Image.open(io.BytesIO(cl.get(ii["thumburl"], timeout=60).content)).convert("RGB")
            except Exception as e: print("  download error", n, e); missing.append(n); continue
            ar = W / H; w, h = img.size                       # center-crop to 16:10, then resize
            if w / h > ar: nw = int(h * ar); img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
            else: nh = int(w / ar); img = img.crop((0, (h - nh) // 3, w, (h - nh) // 3 + nh))   # food is usually a bit above centre
            img.resize((W, H), Image.LANCZOS).save(f, "WEBP", quality=72, method=6)
            used.add(ii["thumburl"])
            credits[n] = {"file": f.name, "title": p["title"], "author": strip(md.get("Artist", {}).get("value")) or "unknown", "license": lic,
                          "source": ii.get("descriptionurl"), "thumb": ii["thumburl"], "match": round(score, 2)}
            it["image"] = f"/menu-images/{f.name}"
            print(f"ok  {n:34} {f.stat().st_size//1024:3d} KB  {lic:12} score={score:.2f}  {p['title'][:60]}")
    CREDITS.write_text(json.dumps(credits, indent=1, ensure_ascii=False))
    write_credits_html(credits)
    if not a.only: MENU.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(credits)} cached, missing: {missing or 'none'}")

if __name__ == "__main__": main()
