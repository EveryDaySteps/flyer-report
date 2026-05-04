#!/usr/bin/env python3
"""
generate_flyer_keyword_report_v4.py

find_flyer_keywords_v5.py の出力ディレクトリからHTMLレポートを生成します。

v2:
- 一覧の検出カードをクリック可能にする
- クリックすると詳細ビューに切り替える
- 該当するチラシ全体画像だけを表示する
- 可能な範囲で、検出位置が画面中央に来るようにスクロールする
"""

from __future__ import annotations

import argparse
import base64
import html
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


def image_to_data_uri(path: str | Path | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    mime = "image/jpeg" if p.suffix.lower() in [".jpg", ".jpeg"] else "image/png"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode('ascii')}"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_hits(output_dir: Path) -> list[dict[str, Any]]:
    data = json.loads((output_dir / "keyword_hits.json").read_text(encoding="utf-8"))
    hits = []
    for i, d in enumerate(data, start=1):
        crop_bbox = d.get("crop_bbox") or [0, 0, 0, 0]
        bbox = d.get("bbox") or [0, 0, 0, 0]
        hits.append({
            "index": i,
            "hit_id": str(d.get("hit_id", f"hit_{i:03d}")),
            "store": str(d.get("store_name") or "店舗名未設定"),
            "source_image": str(d.get("source_image") or ""),
            "category": str(d.get("category") or ""),
            "keyword": str(d.get("keyword") or ""),
            "matched_text": str(d.get("matched_text") or ""),
            "line_text": str(d.get("line_text") or ""),
            "confidence": str(d.get("confidence") or ""),
            "note": str(d.get("note") or ""),
            "crop_path": d.get("crop_path"),
            "bbox": bbox,
            "crop_bbox": crop_bbox,
            "center_x": (crop_bbox[0] + crop_bbox[2]) / 2,
            "center_y": (crop_bbox[1] + crop_bbox[3]) / 2,
        })
    return hits


def get_font(size: int):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/meiryob.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            pass
    return None


def create_highlight_image(source_image: Path, hits: list[dict[str, Any]], out_path: Path) -> Path | None:
    if not source_image.exists():
        return None
    try:
        img = Image.open(source_image).convert("RGBA")
    except Exception:
        return None

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    draw = ImageDraw.Draw(img)
    font_big = get_font(54)

    for hit in hits:
        x1, y1, x2, y2 = [int(v) for v in hit["crop_bbox"]]
        od.rectangle([x1, y1, x2, y2], fill=(255, 230, 0, 95))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    for hit in hits:
        x1, y1, x2, y2 = [int(v) for v in hit["crop_bbox"]]
        bx1, by1, bx2, by2 = [int(v) for v in hit["bbox"]]

        for t in range(5):
            draw.rectangle([x1 - 10 - t, y1 - 10 - t, x2 + 10 + t, y2 + 10 + t], outline=(0, 0, 0, 230))
        for t in range(8):
            draw.rectangle([x1 - t, y1 - t, x2 + t, y2 + t], outline=(250, 204, 21, 255))
        for t in range(7):
            draw.rectangle([bx1 - t, by1 - t, bx2 + t, by2 + t], outline=(255, 0, 0, 255))

        lx, ly = max(0, x1), max(0, y1 - 66)
        draw.rounded_rectangle([lx, ly, lx + 88, ly + 62], radius=14, fill=(220, 38, 38, 255))
        draw.text((lx + 20, ly + 2), str(hit["index"]), fill=(255, 255, 255, 255), font=font_big)

    ensure_dir(out_path.parent)
    img.convert("RGB").save(out_path, quality=92)
    return out_path


def build_report(output_dir: Path, report_name: str) -> Path:
    jst = timezone(timedelta(hours=9))
    generated_at = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")
    hits = load_hits(output_dir)
    report_dir = output_dir / "report"
    highlighted_dir = report_dir / "highlighted"
    ensure_dir(highlighted_dir)

    by_image: dict[str, list[dict[str, Any]]] = {}
    for hit in hits:
        by_image.setdefault(hit["source_image"], []).append(hit)

    images = []
    for source, image_hits in by_image.items():
        src = Path(source)
        out_img = highlighted_dir / f"{src.stem}_highlight.jpg"
        highlighted = create_highlight_image(src, image_hits, out_img)
        w = h = 0
        try:
            with Image.open(src) as im:
                w, h = im.size
        except Exception:
            pass
        images.append({
            "source_image": source,
            "title": src.name,
            "store": image_hits[0]["store"] if image_hits else "店舗名未設定",
            "width": w,
            "height": h,
            "uri": image_to_data_uri(highlighted if highlighted else src),
        })

    records = []
    for hit in hits:
        r = dict(hit)
        r["crop_uri"] = image_to_data_uri(hit.get("crop_path"))
        records.append(r)

    stores = sorted({r["store"] for r in records})
    cats = sorted({r["category"] for r in records})
    keywords = sorted({r["keyword"] for r in records})

    store_options = "".join(f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in stores)
    cat_options = "".join(f'<option value="{html.escape(c)}">{html.escape(c)}</option>' for c in cats)

    data_json = json.dumps(records, ensure_ascii=False)
    images_json = json.dumps(images, ensure_ascii=False)

    html_doc = f'''<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>チラシ キーワード検出レポート</title>
<style>
:root{{--bg:#f6f7fb;--text:#172033;--muted:#667085;--line:#e6e8ef;--brand:#2563eb;--brand-bg:#eff6ff;--shadow:0 8px 22px rgba(15,23,42,.08)}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif}}
header{{position:sticky;top:0;z-index:30;color:white;background:linear-gradient(135deg,#172554,#2563eb);padding:16px 14px;box-shadow:0 2px 10px rgba(0,0,0,.22)}}
header h1{{margin:0;font-size:18px}} header p{{margin:4px 0 0;font-size:12px;opacity:.88}}
main{{max-width:1180px;margin:0 auto;padding:12px}} .summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:4px 0 12px}}
.summary-card{{background:white;border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:12px 10px}} .label{{color:var(--muted);font-size:12px}} .value{{font-size:26px;font-weight:900}}
.controls{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}} .controls input,.controls select{{border:1px solid var(--line);border-radius:999px;background:white;padding:10px 13px;font-size:14px;min-height:40px}} .controls input{{flex:1;min-width:220px}}
.tabs{{display:flex;gap:8px;overflow-x:auto;margin:12px 0}} .tab{{border:1px solid var(--line);background:white;color:#344054;border-radius:999px;padding:9px 13px;font-weight:800;cursor:pointer}} .tab.active{{background:var(--brand);color:white;border-color:var(--brand)}}
.section-title{{margin:14px 2px 9px}} .section-title h2{{margin:0;font-size:17px}} .section-title p{{margin:4px 0 0;color:var(--muted);font-size:12px}}
.store-list{{display:flex;flex-direction:column;gap:18px}} .store-card{{background:white;border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow);overflow:hidden}}
.store-head{{padding:16px 18px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#fff,#f8fbff);display:flex;justify-content:space-between;gap:10px}} .store-head h3{{margin:0;font-size:20px}} .store-meta{{font-size:13px;color:var(--muted);font-weight:700}}
.keyword-list{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;padding:14px}} .keyword-card{{border:1px solid var(--line);border-radius:18px;background:white;overflow:hidden}}
.keyword-head{{padding:12px 13px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}} .keyword-title{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}} .keyword-title h4{{margin:0;font-size:18px}}
.count-badge{{background:var(--brand-bg);color:var(--brand);font-weight:900;border-radius:999px;padding:4px 9px;font-size:12px}} .category-badge{{background:#f3f4f6;color:#374151;font-weight:900;border-radius:999px;padding:4px 9px;font-size:12px}}
.thumb-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;padding:10px;background:#fafafa}} .thumb-item{{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:white;position:relative;cursor:pointer;transition:.12s transform,.12s box-shadow}}
.thumb-item:hover{{transform:translateY(-2px);box-shadow:0 8px 18px rgba(15,23,42,.14)}} .thumb{{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;background:#eef2f7}}
.num{{position:absolute;left:6px;top:6px;background:#dc2626;color:white;font-weight:900;border-radius:999px;padding:4px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.2)}}
.hit-info{{padding:9px 10px;border-top:1px solid var(--line)}} .ocr{{color:#344054;font-size:13px;line-height:1.45;word-break:break-all}} .note{{margin-top:5px;color:var(--muted);font-size:11px;word-break:break-all}}
.detail-toolbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}} .detail-toolbar button{{border:1px solid var(--line);background:white;border-radius:999px;padding:8px 12px;font-weight:800;cursor:pointer}} .hint{{font-size:12px;color:var(--muted)}}
.detail{{background:white;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);overflow:hidden;margin-bottom:18px}} .detail-head{{padding:12px 14px;border-bottom:1px solid var(--line)}} .detail-head h2{{margin:0;font-size:17px}}
.legend{{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}} .legend span{{font-size:12px;padding:4px 8px;border-radius:999px;font-weight:800}} .legend .yellow{{background:#fef3c7;color:#92400e}} .legend .red{{background:#fee2e2;color:#991b1b}}
.detail-img-wrap{{position:relative;background:#111;overflow:auto;max-height:82vh;padding:10px;border-radius:0 0 18px 18px;touch-action:pan-x pan-y;-webkit-overflow-scrolling:touch}}
.detail-img{{display:block;width:max-content;max-width:none;height:auto;border-radius:8px;transform-origin:top left;user-select:none;-webkit-user-select:none}}
.zoom-overlay{{position:sticky;top:10px;left:10px;z-index:10;display:flex;gap:6px;align-items:center;width:max-content;background:rgba(17,24,39,.82);backdrop-filter:blur(6px);border:1px solid rgba(255,255,255,.18);border-radius:999px;padding:7px 8px;margin-bottom:8px;touch-action:manipulation}}
.zoom-overlay button{{border:1px solid rgba(255,255,255,.2);background:white;color:#111827;border-radius:999px;padding:7px 10px;font-weight:900;cursor:pointer;min-width:38px}}
.zoom-overlay .zoom-label{{color:white;font-size:12px;font-weight:900;min-width:46px;text-align:center}}
.hidden{{display:none!important}} .empty{{background:white;border:1px solid var(--line);border-radius:16px;padding:28px;text-align:center;color:var(--muted)}}
@media(max-width:680px){{main{{padding:10px}}.summary{{gap:7px}}.summary-card{{padding:10px 8px}}.value{{font-size:22px}}.keyword-list{{grid-template-columns:1fr;padding:10px}}.thumb-grid{{grid-template-columns:repeat(2,1fr)}}.store-head{{align-items:flex-start;flex-direction:column}}}}
</style></head><body>
<header>
  <h1>チラシ キーワード検出レポート</h1>
  <p>生成日時: {generated_at}</p>
  <p>検出カードをクリックすると、該当チラシの検出位置を中心に表示します。詳細画像は1本指でスクロール、2本指で拡大縮小できます。</p>
</header>
<main>
<div class="summary"><div class="summary-card"><div class="label">検出件数</div><div class="value">{len(records)}</div></div><div class="summary-card"><div class="label">キーワード</div><div class="value">{len(keywords)}</div></div><div class="summary-card"><div class="label">店舗</div><div class="value">{len(stores)}</div></div></div>
<div class="controls"><input id="search" type="search" placeholder="キーワード・OCR文字列で検索"><select id="storeFilter"><option value="">全店舗</option>{store_options}</select><select id="categoryFilter"><option value="">全カテゴリ</option>{cat_options}</select></div>
<div class="tabs"><button class="tab active" data-view="summaryView">全体レポート</button><button class="tab" data-view="detailView">店舗別詳細</button></div>
<section id="summaryView"><div class="section-title"><h2>店舗別・キーワード別まとめ <span id="groupCount"></span></h2><p>カードをクリックすると、該当するチラシ全体と検出位置を確認できます。</p></div><div id="stores" class="store-list"></div></section>
<section id="detailView" class="hidden"><div class="section-title"><h2>チラシ上の検出箇所</h2><p>黄色の半透明領域が切り抜き範囲、赤枠がOCR上の検出文字列です。</p></div><div class="detail-toolbar"><button onclick="showView('summaryView')">一覧に戻る</button><span id="detailHint" class="hint"></span></div><div id="details"></div></section>
</main>
<script>
const DATA = {data_json};
const IMAGES = {images_json};
let currentZoom = 1.0;
let pendingFocusHit = null;
function esc(s){{return String(s??"").replace(/[&<>"']/g,m=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]));}}
function currentRows(){{const q=document.getElementById('search').value.trim().toLowerCase();const store=document.getElementById('storeFilter').value;const cat=document.getElementById('categoryFilter').value;return DATA.filter(r=>{{const hay=`${{r.keyword}} ${{r.matched_text}} ${{r.line_text}} ${{r.store}} ${{r.category}}`.toLowerCase();return (!q||hay.includes(q))&&(!store||r.store===store)&&(!cat||r.category===cat);}});}}
function groupByStoreKeyword(rows){{const stores=new Map();for(const r of rows){{if(!stores.has(r.store)) stores.set(r.store,new Map());const km=stores.get(r.store);const key=`${{r.category}}||${{r.keyword}}`;if(!km.has(key)) km.set(key,{{store:r.store,category:r.category,keyword:r.keyword,items:[]}});km.get(key).items.push(r);}}return [...stores.entries()].map(([store,km])=>({{store,groups:[...km.values()].sort((a,b)=>a.keyword.localeCompare(b.keyword,'ja'))}}));}}
function renderSummary(){{const rows=currentRows();const grouped=groupByStoreKeyword(rows);const groupCount=grouped.reduce((s,g)=>s+g.groups.length,0);document.getElementById('groupCount').textContent=`（${{groupCount}}グループ / ${{rows.length}}件）`;const root=document.getElementById('stores');if(!rows.length){{root.innerHTML='<div class="empty">該当する検出結果がありません</div>';return;}}root.innerHTML=grouped.map(store=>`<article class="store-card"><div class="store-head"><h3>${{esc(store.store)}}</h3><div class="store-meta">${{store.groups.length}}キーワード / ${{store.groups.reduce((s,g)=>s+g.items.length,0)}}件</div></div><div class="keyword-list">${{store.groups.map(g=>`<section class="keyword-card"><div class="keyword-head"><div class="keyword-title"><h4>${{esc(g.keyword)}}</h4><span class="count-badge">${{g.items.length}}件</span></div><span class="category-badge">${{esc(g.category)}}</span></div><div class="thumb-grid">${{g.items.map(r=>`<div class="thumb-item" onclick="openHit(${{r.index}})" title="クリックでチラシ全体を表示"><span class="num">#${{r.index}}</span><img class="thumb" src="${{r.crop_uri}}" alt="${{esc(r.keyword)}}"><div class="hit-info"><div class="ocr"><strong>OCR:</strong> ${{esc(r.matched_text)}}</div><div class="note">${{esc(r.hit_id)}} / ${{esc(r.note)}}</div></div></div>`).join('')}}</div></section>`).join('')}}</div></article>`).join('');}}
function renderDetails(){{const root=document.getElementById('details');if(!IMAGES.length){{root.innerHTML='<div class="empty">詳細画像がありません</div>';return;}}root.innerHTML=IMAGES.map((img,idx)=>`<div class="detail" id="detail-card-${{idx}}" data-source="${{esc(img.source_image)}}"><div class="detail-head"><h2>${{esc(img.store)}} / ${{esc(img.title)}}</h2><p>番号は一覧のサムネイル番号に対応します。</p><div class="legend"><span class="yellow">黄色: 周辺クロップ範囲</span><span class="red">赤: 検出文字</span></div></div><div class="detail-img-wrap" id="detail-wrap-${{idx}}" onwheel="handleWheelZoom(event, ${{idx}})" ondblclick="toggleZoom(${{idx}})">
    <div class="zoom-overlay">
      <button onclick="zoomOut(event)">−</button>
      <button onclick="zoomIn(event)">＋</button>
      <button onclick="resetZoom(event)">等倍</button>
      <span class="zoom-label" id="zoom-label-${{idx}}">100%</span>
    </div>
    <img class="detail-img" id="detail-img-${{idx}}" src="${{img.uri}}" data-width="${{img.width}}" data-height="${{img.height}}" alt="${{esc(img.title)}}">
   </div></div>`).join('');}}
function showView(id){{document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.view===id));document.getElementById('summaryView').classList.toggle('hidden',id!=='summaryView');document.getElementById('detailView').classList.toggle('hidden',id!=='detailView');if(id==='detailView'&&pendingFocusHit){{setTimeout(()=>focusHit(pendingFocusHit),120);}}}}
function clampZoom(z){{return Math.max(0.35,Math.min(3.0,z));}}
function applyZoomToImage(img,z){{currentZoom=clampZoom(z);img.style.transform=`scale(${{currentZoom}})`;img.style.marginBottom=`${{Math.max(0,(currentZoom-1)*img.naturalHeight)}}px`;img.style.marginRight=`${{Math.max(0,(currentZoom-1)*img.naturalWidth)}}px`;const idx=img.id.replace('detail-img-','');const label=document.getElementById(`zoom-label-${{idx}}`);if(label) label.textContent=`${{Math.round(currentZoom*100)}}%`;}}
function setZoom(z){{currentZoom=clampZoom(z);document.querySelectorAll('.detail-img').forEach(img=>applyZoomToImage(img,currentZoom));if(pendingFocusHit) setTimeout(()=>focusHit(pendingFocusHit),80);}}
function activeImage(){{if(!pendingFocusHit) return document.querySelector('.detail:not(.hidden) .detail-img')||document.querySelector('.detail-img');const detailIndex=IMAGES.findIndex(img=>img.source_image===pendingFocusHit.source_image);return document.getElementById(`detail-img-${{detailIndex}}`);}}
function zoomIn(e){{if(e) e.stopPropagation();const img=activeImage();if(!img) return;applyZoomToImage(img,currentZoom+0.2);if(pendingFocusHit) setTimeout(()=>focusHit(pendingFocusHit),60);}}
function zoomOut(e){{if(e) e.stopPropagation();const img=activeImage();if(!img) return;applyZoomToImage(img,currentZoom-0.2);if(pendingFocusHit) setTimeout(()=>focusHit(pendingFocusHit),60);}}
function resetZoom(e){{if(e) e.stopPropagation();const img=activeImage();if(!img) return;applyZoomToImage(img,1.0);if(pendingFocusHit) setTimeout(()=>focusHit(pendingFocusHit),60);}}
function toggleZoom(idx){{const img=document.getElementById(`detail-img-${{idx}}`);if(!img) return;applyZoomToImage(img,currentZoom<1.3?1.6:1.0);if(pendingFocusHit) setTimeout(()=>focusHit(pendingFocusHit),60);}}
function handleWheelZoom(event,idx){{if(!event.ctrlKey&&!event.metaKey) return;event.preventDefault();const img=document.getElementById(`detail-img-${{idx}}`);if(!img) return;const delta=event.deltaY<0?0.12:-0.12;applyZoomToImage(img,currentZoom+delta);}}
let pinchState=null;
function distance(t1,t2){{const dx=t1.clientX-t2.clientX;const dy=t1.clientY-t2.clientY;return Math.sqrt(dx*dx+dy*dy);}}
function setupPinchZoom(){{document.querySelectorAll('.detail-img-wrap').forEach((wrap,idx)=>{{wrap.addEventListener('touchstart',(e)=>{{if(e.touches.length===2){{e.preventDefault();pinchState={{idx:idx,startDist:distance(e.touches[0],e.touches[1]),startZoom:currentZoom}};}}}},{{passive:false}});wrap.addEventListener('touchmove',(e)=>{{if(e.touches.length===2&&pinchState&&pinchState.idx===idx){{e.preventDefault();const img=document.getElementById(`detail-img-${{idx}}`);if(!img) return;const newDist=distance(e.touches[0],e.touches[1]);const ratio=newDist/Math.max(1,pinchState.startDist);applyZoomToImage(img,pinchState.startZoom*ratio);}}}},{{passive:false}});wrap.addEventListener('touchend',()=>{{pinchState=null;}},{{passive:false}});}});}}
function openHit(index){{const hit=DATA.find(r=>r.index===index);if(!hit) return;pendingFocusHit=hit;document.getElementById('detailHint').textContent=`#${{hit.index}} ${{hit.store}} / ${{hit.keyword}} / OCR: ${{hit.matched_text}}`;showView('detailView');}}
function focusHit(hit){{const detailIndex=IMAGES.findIndex(img=>img.source_image===hit.source_image);if(detailIndex<0) return;document.querySelectorAll('.detail').forEach((d,i)=>d.classList.toggle('hidden',i!==detailIndex));const card=document.getElementById(`detail-card-${{detailIndex}}`);const wrap=document.getElementById(`detail-wrap-${{detailIndex}}`);const img=document.getElementById(`detail-img-${{detailIndex}}`);if(!card||!wrap||!img) return;card.scrollIntoView({{behavior:'smooth',block:'start'}});const naturalW=Number(img.dataset.width||img.naturalWidth||1);const naturalH=Number(img.dataset.height||img.naturalHeight||1);const renderedW=img.naturalWidth||naturalW;const renderedH=img.naturalHeight||naturalH;const scaleX=renderedW/naturalW*currentZoom;const scaleY=renderedH/naturalH*currentZoom;const targetX=hit.center_x*scaleX;const targetY=hit.center_y*scaleY;setTimeout(()=>{{wrap.scrollLeft=Math.max(0,targetX-wrap.clientWidth/2);wrap.scrollTop=Math.max(0,targetY-wrap.clientHeight/2);}},160);}}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>showView(t.dataset.view)));['search','storeFilter','categoryFilter'].forEach(id=>document.getElementById(id).addEventListener('input',renderSummary));renderSummary();renderDetails();setupPinchZoom();setZoom(1);
</script></body></html>'''

    out = output_dir / report_name
    out.write_text(html_doc, encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate flyer keyword HTML report v4.")
    parser.add_argument("output_dir", help="Output directory from find_flyer_keywords_v5.py")
    parser.add_argument("--report-name", default="flyer_keyword_report.html")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    report = build_report(output_dir, args.report_name)
    print(f"HTML: {report}")
    print(f"Highlighted images: {output_dir / 'report' / 'highlighted'}")


if __name__ == "__main__":
    main()
