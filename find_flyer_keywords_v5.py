#!/usr/bin/env python3
"""
find_flyer_keywords_v5.py

OCR.spaceでチラシ画像から文字＋座標を取得し、
キーワード辞書に一致する文字列を探して、その周辺画像をクロップするツール。

v3:
- キーワードごとに exact / fuzzy を指定可能
- fuzzy指定したキーワードだけOCR誤読に対応
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import cv2
import requests
import yaml
from PIL import Image


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Word:
    text: str
    bbox: list[int]
    line_text: str
    tile_id: str


@dataclass
class KeywordHit:
    hit_id: str
    source_image: str
    store_name: str | None
    category: str
    keyword: str
    matched_text: str
    line_text: str
    bbox: list[int]
    crop_bbox: list[int]
    crop_path: str | None
    confidence: str
    note: str | None = None


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(v, hi))


def bbox_area(b: list[int]) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def bbox_union(boxes: list[list[int]]) -> list[int]:
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def center(b: list[int]) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def iou(a: list[int], b: list[int]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = bbox_area([x1, y1, x2, y2])
    if inter <= 0:
        return 0.0
    return inter / max(1, bbox_area(a) + bbox_area(b) - inter)


def normalize_text(text: str) -> str:
    t = text.strip()
    t = t.replace(" ", "")
    t = t.replace("　", "")
    t = t.replace("、", "")
    t = t.replace(",", "")
    t = t.replace("・", "")
    t = t.replace("･", "")
    t = t.replace("，", "")
    t = t.replace("ー", "")
    t = t.replace("-", "")
    t = t.replace("−", "")
    t = t.lower()

    # OCRで起きやすい揺れを軽く吸収
    t = t.replace("ッ", "ツ")  # ダッツ/ダツツ系の揺れを弱く吸収
    return t


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def iter_images(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in IMAGE_EXTS:
            raise ValueError(f"Unsupported image file: {input_path}")
        return [input_path]
    if not input_path.is_dir():
        raise ValueError(f"Input path does not exist: {input_path}")
    return [p for p in sorted(input_path.rglob("*")) if p.suffix.lower() in IMAGE_EXTS]


def resolve_store_name(image_path: Path, input_path: Path, explicit_store_name: str | None) -> str | None:
    """
    v5:
    - --store-name が指定されていれば、それを優先
    - inputがフォルダの場合、input直下の1階層目フォルダ名を店舗名にする
      例: flyer_images/カスミ結城店/a.jpg -> カスミ結城店
    - input直下に画像がある場合は親フォルダ名を店舗名にする
    - inputが単一ファイルの場合は、--store-name がなければ親フォルダ名
    """
    if explicit_store_name:
        return explicit_store_name

    try:
        if input_path.is_dir():
            rel = image_path.relative_to(input_path)
            if len(rel.parts) >= 2:
                return rel.parts[0]
            return image_path.parent.name
        return image_path.parent.name
    except Exception:
        return image_path.parent.name if image_path.parent else None


def load_keywords(path: Path) -> dict[str, dict[str, list[str]]]:
    """
    YAML形式:

    アイス:
      exact:
        - アイス
        - pino
      fuzzy:
        - ガツンとみかん
        - ハーゲンダッツ

    後方互換:
    アイス:
      - アイス
      - ハーゲンダッツ

    の場合は、すべて exact として扱います。
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("keyword YAML must be a mapping.")

    categories: dict[str, dict[str, list[str]]] = {}

    for category, values in data.items():
        category_name = str(category)

        if isinstance(values, list):
            # 旧形式は安全側で exact 扱い
            categories[category_name] = {
                "exact": [str(v) for v in values],
                "fuzzy": [],
            }

        elif isinstance(values, dict):
            exact_values = values.get("exact", [])
            fuzzy_values = values.get("fuzzy", [])

            # 旧形式 { keywords: [...] } も exact 扱い
            if "keywords" in values:
                exact_values = values.get("keywords", [])

            if exact_values is None:
                exact_values = []
            if fuzzy_values is None:
                fuzzy_values = []

            if not isinstance(exact_values, list) or not isinstance(fuzzy_values, list):
                raise ValueError(f"exact/fuzzy must be lists: {category_name}")

            categories[category_name] = {
                "exact": [str(v) for v in exact_values],
                "fuzzy": [str(v) for v in fuzzy_values],
            }

        else:
            raise ValueError(f"Invalid keyword entry: {category_name}")

    return categories

def make_tiles(image_path: Path, tiles_dir: Path, cols: int, rows: int, overlap: float) -> list[dict[str, Any]]:
    ensure_dir(tiles_dir)
    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    ox = int(W * overlap)
    oy = int(H * overlap)

    tiles = []
    for r in range(rows):
        for c in range(cols):
            x1 = clamp(int(c * W / cols) - ox, 0, W)
            y1 = clamp(int(r * H / rows) - oy, 0, H)
            x2 = clamp(int((c + 1) * W / cols) + ox, 0, W)
            y2 = clamp(int((r + 1) * H / rows) + oy, 0, H)

            tile_id = f"r{r}_c{c}"
            tile_path = tiles_dir / f"{image_path.stem}_{tile_id}.jpg"
            img.crop((x1, y1, x2, y2)).save(tile_path, quality=92)

            tiles.append({
                "id": tile_id,
                "path": tile_path,
                "origin": [x1, y1],
                "size": [x2 - x1, y2 - y1],
            })
    return tiles


def compress_for_ocr(src: Path, dst: Path, max_bytes: int, max_side: int) -> float:
    img = Image.open(src).convert("RGB")
    ow, oh = img.size

    scale_down = min(1.0, max_side / max(ow, oh))
    if scale_down < 1.0:
        img = img.resize((int(ow * scale_down), int(oh * scale_down)), Image.LANCZOS)

    quality = 90
    while quality >= 40:
        img.save(dst, "JPEG", quality=quality, optimize=True)
        if dst.stat().st_size <= max_bytes:
            break
        quality -= 8

    cw, _ = img.size
    return ow / cw


def call_ocrspace(image_path: Path, api_key: str, language: str, engine: int, timeout: int = 120) -> dict[str, Any]:
    with image_path.open("rb") as f:
        response = requests.post(
            "https://api.ocr.space/parse/image",
            files={"file": (image_path.name, f, "image/jpeg")},
            data={
                "apikey": api_key,
                "language": language,
                "OCREngine": str(engine),
                "isOverlayRequired": "true",
                "scale": "true",
                "detectOrientation": "true",
            },
            timeout=timeout,
        )

    response.raise_for_status()
    data = response.json()
    if data.get("IsErroredOnProcessing"):
        raise RuntimeError(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def extract_words(ocr_result: dict[str, Any], tile_id: str, origin: list[int], scale_to_tile: float) -> list[Word]:
    ox, oy = origin
    words: list[Word] = []

    for pr in ocr_result.get("ParsedResults") or []:
        overlay = pr.get("TextOverlay") or {}
        for line in overlay.get("Lines") or []:
            line_text = (line.get("LineText") or "").strip()
            for w in line.get("Words") or []:
                text = (w.get("WordText") or "").strip()
                if not text:
                    continue

                left = int(round((w.get("Left") or 0) * scale_to_tile)) + ox
                top = int(round((w.get("Top") or 0) * scale_to_tile)) + oy
                width = int(round((w.get("Width") or 0) * scale_to_tile))
                height = int(round((w.get("Height") or 0) * scale_to_tile))

                if width <= 0 or height <= 0:
                    continue

                words.append(Word(text=text, bbox=[left, top, left + width, top + height], line_text=line_text, tile_id=tile_id))
    return words


def best_window_similarity(line_norm: str, key_norm: str) -> float:
    if not line_norm or not key_norm:
        return 0.0
    klen = len(key_norm)
    if len(line_norm) <= klen:
        return SequenceMatcher(None, line_norm, key_norm).ratio()

    best = 0.0
    # 少し長さ違いも許す
    for win_len in range(max(2, klen - 2), min(len(line_norm), klen + 3) + 1):
        for i in range(0, len(line_norm) - win_len + 1):
            window = line_norm[i:i + win_len]
            best = max(best, SequenceMatcher(None, window, key_norm).ratio())
    return best


def is_keyword_match(word_text: str, line_text: str, keyword: str, fuzzy_threshold: float, enable_fuzzy: bool) -> tuple[bool, str, str]:
    word_norm = normalize_text(word_text)
    line_norm = normalize_text(line_text)
    key_norm = normalize_text(keyword)

    if not key_norm:
        return False, "", ""

    if key_norm in word_norm:
        return True, "exact_word", "word"
    if key_norm in line_norm:
        return True, "exact_line", "line"

    if not enable_fuzzy:
        return False, "", ""

    # 短すぎるキーワードは誤爆しやすい
    if len(key_norm) < 3:
        return False, "", ""

    if len(word_norm) >= 2:
        sim_word = SequenceMatcher(None, word_norm, key_norm).ratio()
        if sim_word >= fuzzy_threshold:
            return True, f"fuzzy_word:{sim_word:.2f}", "word"

    # 長いOCR行に対しては部分窓の類似度を見る
    if len(line_norm) >= 3:
        sim_line = best_window_similarity(line_norm, key_norm)
        if sim_line >= fuzzy_threshold:
            return True, f"fuzzy_line:{sim_line:.2f}", "line"

    return False, "", ""


def find_keyword_hits(
    words: list[Word],
    keywords: dict[str, dict[str, list[str]]],
    image_w: int,
    image_h: int,
    crop_width_ratio: float,
    crop_height_ratio: float,
    fuzzy_threshold: float,
    enable_fuzzy: bool,
    same_line_y_threshold: int,
) -> list[KeywordHit]:
    hits: list[KeywordHit] = []

    # category, keyword, use_fuzzy
    keyword_pairs: list[tuple[str, str, bool]] = []
    for category, block in keywords.items():
        for key in block.get("exact", []):
            keyword_pairs.append((category, key, False))
        for key in block.get("fuzzy", []):
            keyword_pairs.append((category, key, True))

    for word in words:
        for category, keyword, use_fuzzy in keyword_pairs:
            matched, match_type, scope = is_keyword_match(
                word.text,
                word.line_text,
                keyword,
                fuzzy_threshold=fuzzy_threshold,
                enable_fuzzy=(enable_fuzzy and use_fuzzy),
            )

            if not matched:
                continue

            # v4修正:
            # v3では line_text が同じOCR単語を画像全体から集めていたため、
            # 上部の「アイス」と下部の「アイス」が1つの縦長bboxに結合されることがあった。
            # v4では「同じタイル」かつ「Y座標が近い」単語だけを同一行として扱う。
            word_cy = center(word.bbox)[1]
            same_line_boxes = [
                w.bbox
                for w in words
                if w.line_text == word.line_text
                and w.line_text
                and w.tile_id == word.tile_id
                and abs(center(w.bbox)[1] - word_cy) < same_line_y_threshold
            ]

            if scope == "line" and same_line_boxes:
                hit_bbox = bbox_union(same_line_boxes)
                matched_text = word.line_text
            else:
                hit_bbox = word.bbox
                matched_text = word.text

            cx, cy = center(hit_bbox)
            crop_w = int(image_w * crop_width_ratio)
            crop_h = int(image_h * crop_height_ratio)

            crop_bbox = [
                clamp(int(cx - crop_w / 2), 0, image_w),
                clamp(int(cy - crop_h / 2), 0, image_h),
                clamp(int(cx + crop_w / 2), 0, image_w),
                clamp(int(cy + crop_h / 2), 0, image_h),
            ]

            confidence = "high" if match_type.startswith("exact") else "medium"

            hits.append(
                KeywordHit(
                    hit_id="",
                    source_image="",
                    store_name=None,
                    category=category,
                    keyword=keyword,
                    matched_text=matched_text,
                    line_text=word.line_text,
                    bbox=hit_bbox,
                    crop_bbox=crop_bbox,
                    crop_path=None,
                    confidence=confidence,
                    note=f"match_type={match_type}; fuzzy={use_fuzzy}",
                )
            )

    return dedupe_hits(hits)


def dedupe_hits(hits: list[KeywordHit]) -> list[KeywordHit]:
    kept: list[KeywordHit] = []

    def rank(hit: KeywordHit) -> tuple[int, int, int]:
        conf = 0 if hit.confidence == "high" else 1
        return (conf, hit.crop_bbox[1], hit.crop_bbox[0])

    for hit in sorted(hits, key=rank):
        duplicate = False
        for k in kept:
            if hit.category != k.category:
                continue
            overlap = iou(hit.crop_bbox, k.crop_bbox)
            same_text = hit.matched_text == k.matched_text
            same_keyword = hit.keyword == k.keyword

            if same_text and overlap > 0.45:
                duplicate = True
                break
            if same_keyword and overlap > 0.70:
                duplicate = True
                break

        if not duplicate:
            kept.append(hit)

    for i, hit in enumerate(kept, start=1):
        hit.hit_id = f"hit_{i:03d}"

    return kept


def safe_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", text)


def create_hit_crops(image_path: Path, hits: list[KeywordHit], crops_dir: Path, store_name: str | None) -> None:
    ensure_dir(crops_dir)
    img = Image.open(image_path).convert("RGB")

    for hit in hits:
        crop = img.crop(tuple(hit.crop_bbox))
        crop_path = crops_dir / f"{image_path.stem}_{hit.hit_id}_{safe_filename(hit.keyword)}.jpg"
        crop.save(crop_path, quality=92)
        hit.crop_path = str(crop_path)
        hit.source_image = str(image_path)
        hit.store_name = store_name


def draw_debug(image_path: Path, words: list[Word], hits: list[KeywordHit], out_path: Path) -> None:
    img = cv2.imread(str(image_path))
    if img is None:
        return

    for w in words:
        cv2.rectangle(img, (w.bbox[0], w.bbox[1]), (w.bbox[2], w.bbox[3]), (255, 180, 0), 1)

    for i, hit in enumerate(hits, start=1):
        cb = hit.crop_bbox
        hb = hit.bbox
        color = (0, 200, 0) if hit.confidence == "high" else (0, 160, 255)
        cv2.rectangle(img, (cb[0], cb[1]), (cb[2], cb[3]), color, 3)
        cv2.rectangle(img, (hb[0], hb[1]), (hb[2], hb[3]), (0, 0, 255), 2)
        cv2.putText(img, str(i), (cb[0] + 5, max(24, cb[1] + 24)), cv2.FONT_HERSHEY_SIMPLEX, 0.85, color, 2, cv2.LINE_AA)

    cv2.imwrite(str(out_path), img)


def save_json(items: list[Any], path: Path) -> None:
    path.write_text(json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2), encoding="utf-8")


def save_csv(hits: list[KeywordHit], path: Path) -> None:
    fields = [
        "hit_id", "store_name", "source_image", "category", "keyword",
        "matched_text", "line_text", "bbox", "crop_bbox", "crop_path",
        "confidence", "note",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for hit in hits:
            d = asdict(hit)
            d["bbox"] = json.dumps(d["bbox"], ensure_ascii=False)
            d["crop_bbox"] = json.dumps(d["crop_bbox"], ensure_ascii=False)
            writer.writerow({k: d.get(k) for k in fields})


def process_image(image_path: Path, output_dir: Path, keywords: dict[str, list[str]], api_key: str, args: argparse.Namespace, store_name: str | None = None) -> list[KeywordHit]:
    img = Image.open(image_path).convert("RGB")
    W, H = img.size

    tiles_dir = output_dir / "tiles"
    upload_dir = output_dir / "ocr_upload"
    ocr_dir = output_dir / "ocr"
    crops_dir = output_dir / "crops"
    debug_dir = output_dir / "debug"

    for d in [tiles_dir, upload_dir, ocr_dir, crops_dir, debug_dir]:
        ensure_dir(d)

    tiles = make_tiles(image_path, tiles_dir, args.cols, args.rows, args.overlap)
    all_words: list[Word] = []
    raw_results = []

    for tile in tiles:
        upload_path = upload_dir / f"{image_path.stem}_{tile['id']}_upload.jpg"
        scale_to_tile = compress_for_ocr(tile["path"], upload_path, max_bytes=args.max_bytes, max_side=args.max_side)

        print(f"  OCR tile {tile['id']}: {upload_path.stat().st_size // 1024} KB")

        try:
            result = call_ocrspace(upload_path, api_key=api_key, language=args.language, engine=args.engine)
        except Exception as e:
            print(f"  OCR ERROR tile {tile['id']}: {e}")
            continue

        raw_results.append({"tile": tile["id"], "result": result})
        all_words.extend(extract_words(result, tile_id=tile["id"], origin=tile["origin"], scale_to_tile=scale_to_tile))

        if args.sleep_sec > 0:
            time.sleep(args.sleep_sec)

    hits = find_keyword_hits(
        all_words,
        keywords,
        image_w=W,
        image_h=H,
        crop_width_ratio=args.crop_width_ratio,
        crop_height_ratio=args.crop_height_ratio,
        fuzzy_threshold=args.fuzzy_threshold,
        enable_fuzzy=not args.disable_fuzzy,
        same_line_y_threshold=args.same_line_y_threshold,
    )

    create_hit_crops(image_path, hits, crops_dir, store_name)
    draw_debug(image_path, all_words, hits, debug_dir / f"{image_path.stem}_keyword_debug.jpg")

    save_json(all_words, ocr_dir / f"{image_path.stem}_ocr_words.json")
    (ocr_dir / f"{image_path.stem}_ocr_raw_by_tile.json").write_text(json.dumps(raw_results, ensure_ascii=False, indent=2), encoding="utf-8")

    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description="Find flyer keywords using OCR.space. v5 with auto store name from parent folder.")
    parser.add_argument("input", help="Input image file or directory")
    parser.add_argument("--keywords", default="./flyer_keywords.yaml", help="Keyword YAML file")
    parser.add_argument("--output-dir", default="./keyword_output_v5")
    parser.add_argument("--store-name", default=None, help="指定した場合は全画像にこの店舗名を設定。未指定なら親フォルダ名を店舗名として自動設定します。")
    parser.add_argument("--cols", type=int, default=2)
    parser.add_argument("--rows", type=int, default=2)
    parser.add_argument("--overlap", type=float, default=0.06)
    parser.add_argument("--language", default="jpn")
    parser.add_argument("--engine", type=int, default=1, choices=[1, 2, 3])
    parser.add_argument("--max-bytes", type=int, default=950000)
    parser.add_argument("--max-side", type=int, default=2200)
    parser.add_argument("--sleep-sec", type=float, default=0.2)
    parser.add_argument("--crop-width-ratio", type=float, default=0.22)
    parser.add_argument("--crop-height-ratio", type=float, default=0.18)
    parser.add_argument("--fuzzy-threshold", type=float, default=0.72)
    parser.add_argument("--disable-fuzzy", action="store_true")
    parser.add_argument("--same-line-y-threshold", type=int, default=80, help="同一行とみなすY座標差。v4の縦長bbox対策。Default: 80")

    args = parser.parse_args()

    api_key = os.environ.get("OCRSPACE_API_KEY")
    if not api_key:
        raise SystemExit("OCRSPACE_API_KEY is not set.")

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    keywords_path = Path(args.keywords)

    ensure_dir(output_dir)

    keywords = load_keywords(keywords_path)
    images = iter_images(input_path)

    all_hits: list[KeywordHit] = []

    print(f"Found {len(images)} image(s)")
    print(f"Keyword categories: {', '.join(keywords.keys())}")
    print(f"Tiles: {args.cols} x {args.rows}")
    print(f"Fuzzy: {'off' if args.disable_fuzzy else 'on'} threshold={args.fuzzy_threshold}")

    for image_path in images:
        store_name = resolve_store_name(image_path, input_path, args.store_name)
        print(f"Processing: {image_path}")
        print(f"  Store: {store_name or '(not set)'}")
        try:
            hits = process_image(
                image_path=image_path,
                output_dir=output_dir,
                keywords=keywords,
                api_key=api_key,
                args=args,
                store_name=store_name,
            )
            print(f"  -> {len(hits)} hit(s)")
            all_hits.extend(hits)
        except Exception as e:
            print(f"  ERROR: {e}")

    save_json(all_hits, output_dir / "keyword_hits.json")
    save_csv(all_hits, output_dir / "keyword_hits.csv")

    print()
    print(f"Done. {len(all_hits)} hit(s) total")
    print(f"CSV:   {output_dir / 'keyword_hits.csv'}")
    print(f"JSON:  {output_dir / 'keyword_hits.json'}")
    print(f"Crops: {output_dir / 'crops'}")
    print(f"Debug: {output_dir / 'debug'}")
    print(f"OCR:   {output_dir / 'ocr'}")


if __name__ == "__main__":
    main()
