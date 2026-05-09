/**
 * scrape_flyers.js
 * 複数店舗のチラシ画像を一括ダウンロードする
 *
 * 処理方式はページ内容から自動判別:
 *   tokubai_widget  … widgets.tokubai.co.jp へのリンクを検出
 *   shufoo          … shufoo / aspViewerRedirect へのリンクを検出
 *   kamashin_direct … img[alt*="チラシ"] を検出（上記に該当しない場合）
 *
 * 設定ファイル : flyer_stores.json  ← 店舗名と URL のみ記載
 * 出力ディレクトリ: ./flyer_images/{店舗名}/
 * マニフェスト  : ./flyer_manifest.json
 */

const { chromium } = require('playwright');
const fs   = require('fs');
const path = require('path');
const https = require('https');
const http  = require('http');

let sharp;
try { sharp = require('sharp'); } catch { sharp = null; }

const STORES       = JSON.parse(fs.readFileSync('./flyer_stores.json', 'utf8'));
const OUTPUT_DIR   = './flyer_images';
const MANIFEST_PATH = './flyer_manifest.json';

// ──────────────────────────────────────────────
// ユーティリティ
// ──────────────────────────────────────────────

function sanitizeDirName(name) {
  return name.replace(/[\/\\:*?"<>|]/g, '_');
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

/** URL からバイト列を取得（リダイレクト追従） */
function downloadBuffer(url, redirects = 0) {
  if (redirects > 5) return Promise.reject(new Error('Too many redirects'));
  return new Promise((resolve, reject) => {
    const proto = url.startsWith('https') ? https : http;
    const chunks = [];
    proto.get(url, {
      headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }
    }, res => {
      if (res.statusCode === 301 || res.statusCode === 302) {
        return downloadBuffer(res.headers.location, redirects + 1).then(resolve).catch(reject);
      }
      if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks)));
    }).on('error', reject);
  });
}

/** URL からファイルに保存（リダイレクト追従） */
function downloadFile(url, dest, redirects = 0) {
  if (redirects > 5) return Promise.reject(new Error('Too many redirects'));
  return new Promise((resolve, reject) => {
    const proto = url.startsWith('https') ? https : http;
    const file = fs.createWriteStream(dest);
    proto.get(url, {
      headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }
    }, res => {
      if (res.statusCode === 301 || res.statusCode === 302) {
        file.close();
        fs.existsSync(dest) && fs.unlinkSync(dest);
        return downloadFile(res.headers.location, dest, redirects + 1).then(resolve).catch(reject);
      }
      if (res.statusCode !== 200) {
        file.close();
        fs.existsSync(dest) && fs.unlinkSync(dest);
        return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
      }
      res.pipe(file);
      file.on('finish', () => { file.close(); resolve(dest); });
    }).on('error', err => {
      fs.existsSync(dest) && fs.unlinkSync(dest);
      reject(err);
    });
  });
}

/**
 * シンプルな XML フィールド抽出（外部ライブラリ不要）
 * @param {string} xml  XML 文字列
 * @param {string} tag  タグ名
 * @returns {string|null}
 */
function xmlField(xml, tag) {
  const m = xml.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`));
  return m ? m[1].trim() : null;
}

/**
 * 繰り返しタグをすべて抽出してその内容文字列の配列を返す
 * @param {string} xml
 * @param {string} tag
 * @returns {string[]}
 */
function xmlArray(xml, tag) {
  const re  = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, 'g');
  const out = [];
  let m;
  while ((m = re.exec(xml)) !== null) out.push(m[0]);
  return out;
}

// ──────────────────────────────────────────────
// 共通: Tokubai print ページから画像をダウンロード
// （tokubai_widget / tokubai_direct 両方で使用）
// ──────────────────────────────────────────────

async function downloadTokubaiLeaflets(store, browser, storeSegment, tokubaiId, leafletIds) {
  const results  = [];
  const storeDir = path.join(OUTPUT_DIR, sanitizeDirName(store.name));
  fs.mkdirSync(storeDir, { recursive: true });

  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
  });

  for (const leafletId of leafletIds) {
    const printUrl = `https://tokubai.co.jp/${storeSegment}/${tokubaiId}/leaflets/${leafletId}/print`;
    console.log(`  Leaflet ${leafletId}: ${printUrl}`);

    try {
      const printPage = await context.newPage();
      try {
        await printPage.goto(printUrl, { waitUntil: 'networkidle', timeout: 30000 });
      } catch { /* timeout 継続 */ }
      await sleep(2000);

      const imgUrls = await printPage.evaluate(() => {
        const imgs = document.querySelectorAll(
          '.printable_leaflet_image, img[src*="bargain_office_leaflets"], img[src*="tokubai"]'
        );
        return Array.from(imgs)
          .map(img => img.src || img.getAttribute('data-src'))
          .filter(src => src && src.startsWith('http'));
      });
      console.log(`    → ${imgUrls.length} image(s) on print page`);

      for (let i = 0; i < imgUrls.length; i++) {
        let hiresUrl = imgUrls[i]
          .replace(/w=\d+,h=\d+,?c?=?t?r?u?e?,?/, '')
          .replace('/images/', '/images/o=true/')
          .replace(/\/o=true\/o=true\//, '/o=true/');

        const imgId = hiresUrl.match(/\/(\d+)\.jpg/)?.[1] || `${leafletId}_${i}`;
        const dest  = path.join(storeDir, `leaflet_${leafletId}_${imgId}.jpg`);

        try {
          await downloadFile(hiresUrl, dest);
          const kb = Math.round(fs.statSync(dest).size / 1024);
          console.log(`    Saved: ${path.basename(dest)} (${kb}KB)`);
          results.push({ store: store.name, leaflet_id: leafletId, path: dest, url: hiresUrl });
        } catch {
          try {
            await downloadFile(imgUrls[i], dest);
            const kb = Math.round(fs.statSync(dest).size / 1024);
            console.log(`    Saved (fallback): ${path.basename(dest)} (${kb}KB)`);
            results.push({ store: store.name, leaflet_id: leafletId, path: dest, url: imgUrls[i] });
          } catch (e2) {
            console.warn(`    Failed: ${e2.message}`);
          }
        }
      }

      if (imgUrls.length === 0) {
        const ssPath = path.join(storeDir, `screenshot_${leafletId}.png`);
        await printPage.screenshot({ path: ssPath, fullPage: true });
        console.log(`    Saved screenshot: ${path.basename(ssPath)}`);
        results.push({ store: store.name, leaflet_id: leafletId, path: ssPath, url: printUrl });
      }

      await printPage.close();
    } catch (e) {
      console.error(`    Error leaflet ${leafletId}: ${e.message}`);
    }
  }

  await context.close();
  return results;
}

// ──────────────────────────────────────────────
// 1. Tokubai Widget（カスミ等）
// ──────────────────────────────────────────────

async function scrapeTokubaiWidget(store, browser) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
  });
  const page = await context.newPage();

  console.log(`  Navigating: ${store.url}`);
  try {
    await page.goto(store.url, { waitUntil: 'networkidle', timeout: 30000 });
  } catch { /* networkidle timeout は無視 */ }
  await sleep(2000);

  // widgets.tokubai.co.jp/{tokubaiId}/leaflet_widget/click?id={leafletId} 形式のリンクを収集
  const widgetLinks = await page.evaluate(() => {
    return Array.from(document.querySelectorAll('a[href*="widgets.tokubai.co.jp"]'))
      .map(a => a.href)
      .filter(h => h.includes('leaflet_widget/click'));
  });
  console.log(`  Found ${widgetLinks.length} widget link(s)`);

  // leafletId・tokubaiId・storeSegment を収集（重複排除）
  const seenLeaflets = new Set();
  let tokubaiId = null;
  let storeSegment = encodeURIComponent(store.name);
  const leafletIds = [];

  for (const link of widgetLinks) {
    const m = link.match(/widgets\.tokubai\.co\.jp\/(\d+)\/leaflet_widget\/click\?id=(\d+)/);
    if (!m) continue;
    if (!tokubaiId) {
      tokubaiId = m[1];
      // リダイレクト先から storeSegment を取得
      try {
        const tmpPage = await context.newPage();
        await tmpPage.goto(link, { waitUntil: 'domcontentloaded', timeout: 15000 });
        const urlM = tmpPage.url().match(/tokubai\.co\.jp\/([^/]+)\/\d+\/leaflets\/\d+\//);
        if (urlM) storeSegment = urlM[1];
        await tmpPage.close();
      } catch { /* 取得失敗は無視 */ }
    }
    const leafletId = m[2];
    if (!seenLeaflets.has(leafletId)) {
      seenLeaflets.add(leafletId);
      leafletIds.push(leafletId);
    }
  }

  await context.close();

  if (!tokubaiId || leafletIds.length === 0) {
    console.warn('  leafletId が取得できませんでした');
    return [];
  }

  return downloadTokubaiLeaflets(store, browser, storeSegment, tokubaiId, leafletIds);
}

// ──────────────────────────────────────────────
// 1b. Tokubai Direct（とりせん等：サイトにウィジェット非掲載）
// ──────────────────────────────────────────────

async function scrapeTokubaiDirect(store, browser) {
  // stores.json に tokubai_store / tokubai_id が必須
  const tokubaiStore = store.tokubai_store;
  const tokubaiId    = store.tokubai_id;
  if (!tokubaiStore || !tokubaiId) {
    console.error('  tokubai_store と tokubai_id が必要です');
    return [];
  }

  // Tokubai の店舗ページを開いて最新の leafletId を収集
  const storeSegment = encodeURIComponent(tokubaiStore);
  const storePageUrl = `https://tokubai.co.jp/${storeSegment}/${tokubaiId}/`;
  console.log(`  Tokubai store page: ${storePageUrl}`);

  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
  });
  const page = await context.newPage();
  try {
    await page.goto(storePageUrl, { waitUntil: 'networkidle', timeout: 30000 });
  } catch { /* timeout 継続 */ }
  await sleep(2000);

  // /leaflets/{leafletId}/ 形式のリンクを収集
  const leafletIds = await page.evaluate((tid) => {
    const seen = new Set();
    const ids  = [];
    document.querySelectorAll(`a[href*="/leaflets/"]`).forEach(a => {
      const m = a.href.match(/\/leaflets\/(\d+)/);
      if (m && !seen.has(m[1])) {
        seen.add(m[1]);
        ids.push(m[1]);
      }
    });
    return ids;
  }, tokubaiId);

  await context.close();
  console.log(`  Found ${leafletIds.length} leaflet(s) on Tokubai`);

  if (leafletIds.length === 0) {
    console.warn('  leafletId が取得できませんでした');
    return [];
  }

  return downloadTokubaiLeaflets(store, browser, storeSegment, tokubaiId, leafletIds);
}

// ──────────────────────────────────────────────
// 2. Shufoo（とりせん等）
// ──────────────────────────────────────────────

async function scrapeShufoo(store, browser) {
  if (!sharp) throw new Error('sharp が必要です: npm install sharp');

  const results  = [];
  const storeDir = path.join(OUTPUT_DIR, sanitizeDirName(store.name));
  fs.mkdirSync(storeDir, { recursive: true });

  // ── 2-1. shopId を取得（設定ファイル優先 → ページ解析）──
  let shopId = store.shufoo_shop_id || null;

  if (shopId) {
    console.log(`  shopId (config): ${shopId}`);
  } else {
    console.log(`  Navigating: ${store.url}`);
    const context = await browser.newContext({
      viewport: { width: 1280, height: 900 },
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    });
    const page = await context.newPage();
    try {
      await page.goto(store.url, { waitUntil: 'networkidle', timeout: 30000 });
    } catch { /* timeout 継続 */ }
    await sleep(2000);

    shopId = await page.evaluate(() => {
      // a タグ・iframe・インラインスクリプトから shopId を探す
      const candidates = [
        ...Array.from(document.querySelectorAll('a[href*="shufoo"], a[href*="aspViewerRedirect"]'))
          .map(el => el.href),
        ...Array.from(document.querySelectorAll('iframe[src*="shufoo"]'))
          .map(el => el.src)
      ];
      for (const href of candidates) {
        const m = href.match(/shopId=(\d+)/);
        if (m) return m[1];
      }
      // ページ HTML 全体を検索（URL形式 shopId=NNN または JS形式 shopId: NNN）
      const m = document.documentElement.innerHTML.match(/shopId[=:]\s*['"]?(\d+)/);
      return m ? m[1] : null;
    });

    await context.close();

    if (!shopId) {
      console.error('  shopId が見つかりませんでした');
      return results;
    }
    console.log(`  shopId: ${shopId}`);
  }

  // ── 2-2. Shufoo API でチラシ一覧取得 ──
  const apiUrl = `https://asp.shufoo.net/api/shopDetailNewXML.php?publisherId=${shopId}&crosstype=asp&useUtf=true`;
  console.log(`  API: ${apiUrl}`);
  const apiXml = (await downloadBuffer(apiUrl)).toString('utf8');

  // <chirashi> ブロックをすべて抽出
  const chirashiBlocks = xmlArray(apiXml, 'chirashi');
  console.log(`  ${chirashiBlocks.length} chirashi found`);

  for (const block of chirashiBlocks) {
    const contentURI   = xmlField(block, 'contentURI');
    const contentsXmlUrl = xmlField(block, 'contentsXml');
    const title        = xmlField(block, 'title') || 'unknown';

    if (!contentURI || !contentsXmlUrl) {
      console.warn(`  Skip: no contentURI or contentsXml for "${title}"`);
      continue;
    }

    // chirashiId を contentURI の末尾セグメントから取得
    const chirashiId = contentURI.replace(/\/$/, '').split('/').pop() || Date.now().toString();
    console.log(`  Chirashi: "${title}" (${chirashiId})`);

    // ── 2-3. contents.xml でタイル情報を取得 ──
    let cXml;
    try {
      cXml = (await downloadBuffer(contentsXmlUrl)).toString('utf8');
    } catch (e) {
      console.warn(`  contents.xml 取得失敗: ${e.message}`);
      continue;
    }

    const totalPages = parseInt(xmlField(cXml, 'totalPages') || '1');
    const bookW   = parseInt(xmlField(cXml, 'bookW')  || '720');
    const bookH   = parseInt(xmlField(cXml, 'bookH')  || '512');
    const sliceW  = parseInt(xmlField(cXml, 'sliceW') || '512');
    const sliceH  = parseInt(xmlField(cXml, 'sliceH') || '512');
    const baseURI = contentURI.endsWith('/') ? contentURI : contentURI + '/';

    // scaleSize="1,2,4,8" → URLスケール値は scaleLevel*100 (100/200/400/800)
    // 最大スケールを選択して最高解像度のタイルを取得する
    const scaleSizeRaw = xmlField(cXml, 'scaleSize') || '1';
    const maxScaleLevel = Math.max(...scaleSizeRaw.split(',').map(Number).filter(n => n > 0));
    const urlScale = maxScaleLevel * 100;  // 例: 4 → 400
    const scaledW  = bookW * maxScaleLevel;
    const scaledH  = bookH * maxScaleLevel;
    const tilesX   = Math.ceil(scaledW / sliceW);
    const tilesY   = Math.ceil(scaledH / sliceH);

    console.log(`  Pages: ${totalPages}, Base: ${bookW}x${bookH}, Scale: ${maxScaleLevel}x → ${scaledW}x${scaledH}, Tiles: ${tilesX}x${tilesY}/page`);

    // ── 2-4. ページごとにタイルを取得・合成 ──
    for (let pageIdx = 0; pageIdx < totalPages; pageIdx++) {
      const dest = path.join(storeDir, `${chirashiId}_p${pageIdx}.jpg`);

      const compositeInputs = [];
      let tileIdx = 0;

      for (let ty = 0; ty < tilesY; ty++) {
        for (let tx = 0; tx < tilesX; tx++) {
          const tileUrl = `${baseURI}index/img/${pageIdx}_${urlScale}_${tileIdx}.jpg`;
          try {
            const buf = await downloadBuffer(tileUrl);
            compositeInputs.push({ input: buf, left: tx * sliceW, top: ty * sliceH });
          } catch (e) {
            console.warn(`    Tile [${pageIdx},${tileIdx}] failed: ${e.message}`);
          }
          tileIdx++;
        }
      }

      // タイルが1枚も取得できなかった場合は scale=100 (1x) にフォールバック
      if (compositeInputs.length === 0 && urlScale !== 100) {
        console.warn(`  スケール ${urlScale} でタイル取得失敗。scale=100 にフォールバック`);
        const tileUrl = `${baseURI}index/img/${pageIdx}_100_0.jpg`;
        try {
          const buf = await downloadBuffer(tileUrl);
          compositeInputs.push({ input: buf, left: 0, top: 0 });
        } catch (e) {
          console.warn(`  Fallback tile failed: ${e.message}`);
        }
      }

      if (compositeInputs.length === 0) {
        console.warn(`  No tiles for page ${pageIdx}`);
        continue;
      }

      // 合成サイズ: 実際のタイル配置に合わせて決定
      const canvasW = compositeInputs.length > 1 ? scaledW : bookW;
      const canvasH = compositeInputs.length > 1 ? scaledH : bookH;

      try {
        await sharp({
          create: {
            width: canvasW, height: canvasH,
            channels: 3, background: { r: 255, g: 255, b: 255 }
          }
        })
          .composite(compositeInputs)
          .jpeg({ quality: 92 })
          .toFile(dest);

        const kb = Math.round(fs.statSync(dest).size / 1024);
        console.log(`  Saved: ${path.basename(dest)} (${kb}KB)  [${canvasW}x${canvasH}px]`);
        results.push({ store: store.name, chirashi_id: chirashiId, page: pageIdx, path: dest, title });
      } catch (e) {
        console.error(`  Sharp error page ${pageIdx}: ${e.message}`);
      }
    }
  }

  return results;
}

// ──────────────────────────────────────────────
// 3. かましん 直接ダウンロード
// ──────────────────────────────────────────────

async function scrapeKamashinDirect(store, browser) {
  const results  = [];
  const storeDir = path.join(OUTPUT_DIR, sanitizeDirName(store.name));
  fs.mkdirSync(storeDir, { recursive: true });

  console.log(`  Navigating: ${store.url}`);
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
  });
  const page = await context.newPage();
  try {
    await page.goto(store.url, { waitUntil: 'networkidle', timeout: 30000 });
  } catch { /* timeout 継続 */ }
  await sleep(2000);

  // alt に "チラシ" を含む img 要素を収集
  const imgData = await page.evaluate(() => {
    return Array.from(document.querySelectorAll('img[alt*="チラシ"]'))
      .map(img => ({ src: img.src || img.getAttribute('data-src') || '', alt: img.alt }))
      .filter(d => d.src.startsWith('http'));
  });

  console.log(`  Found ${imgData.length} flyer image(s)`);
  await context.close();

  for (const { src, alt } of imgData) {
    const filename = decodeURIComponent(src.split('/').pop().split('?')[0]) || 'flyer.jpg';
    const dest     = path.join(storeDir, filename);

    try {
      await downloadFile(src, dest);
      const kb = Math.round(fs.statSync(dest).size / 1024);
      console.log(`  Saved: ${filename} (${kb}KB) [${alt}]`);
      results.push({ store: store.name, path: dest, url: src, alt });
    } catch (e) {
      console.warn(`  Failed: ${e.message}`);
    }
  }

  return results;
}

// ──────────────────────────────────────────────
// 4. 西松屋 (nishimatsuya) — Playwright スクリーンショット方式
// ──────────────────────────────────────────────

async function scrapeNishimatsuya(store, browser) {
  const results  = [];
  const storeDir = path.join(OUTPUT_DIR, sanitizeDirName(store.name));
  fs.mkdirSync(storeDir, { recursive: true });

  // ── Step 1: 店舗ページ (24028.jp) からフリップブック URL を収集 ──
  console.log(`  Navigating: ${store.url}`);
  const listContext = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
  });
  const listPage = await listContext.newPage();
  try {
    await listPage.goto(store.url, { waitUntil: 'networkidle', timeout: 30000 });
  } catch { /* timeout 継続 */ }
  await sleep(2000);

  // nishimatsuya.com/flier/{名前}/ 形式の href を収集（重複排除）
  const flipperUrls = await listPage.evaluate(() => {
    const seen = new Set();
    const urls = [];
    document.querySelectorAll('a[href*="nishimatsuya.com/flier/"]').forEach(a => {
      const href = a.href.split('#')[0].replace(/\/?$/, '/'); // フラグメント除去 + 末尾スラッシュ正規化
      if (!seen.has(href)) {
        seen.add(href);
        urls.push(href);
      }
    });
    return urls;
  });

  await listContext.close();
  console.log(`  Found ${flipperUrls.length} flipbook URL(s)`);

  if (flipperUrls.length === 0) {
    console.warn('  フリップブック URL が見つかりませんでした');
    return results;
  }

  // ── Step 2: 各フリップブックをスクリーンショット ──
  for (const flipperUrl of flipperUrls) {
    // チラシ名を URL から抽出: ".../flier/chirashi_0423_0506/" → "chirashi_0423_0506"
    const flyerName = flipperUrl.replace(/\/$/, '').split('/').pop() || 'unknown';
    console.log(`  Flipbook: ${flyerName}`);

    // ── Step 2a: book.xml からページ数と寸法を取得 ──
    let totalPages  = 1;
    let pageW = 630;
    let pageH = 900;
    try {
      const xmlText = (await downloadBuffer(`${flipperUrl}book.xml`)).toString('utf8');
      totalPages = parseInt(xmlField(xmlText, 'total') || '1', 10);
      pageW      = parseInt(xmlField(xmlText, 'pageWidth')  || '630', 10);
      pageH      = parseInt(xmlField(xmlText, 'pageHeight') || '900', 10);
      console.log(`    book.xml: ${totalPages} page(s), ${pageW}x${pageH}px`);
    } catch (e) {
      console.warn(`    book.xml 取得失敗 (${e.message})、デフォルト値で続行`);
    }

    // ── Step 2b: deviceScaleFactor:2 で高解像度コンテキストを開く ──
    // 実効解像度: pageW*2 × pageH*2 px（例: 1260×1800px）
    const viewerContext = await browser.newContext({
      viewport: { width: pageW, height: pageH },
      deviceScaleFactor: 2,
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    });
    const viewerPage = await viewerContext.newPage();

    try {
      await viewerPage.goto(flipperUrl, { waitUntil: 'networkidle', timeout: 45000 });
    } catch { /* timeout 継続 */ }

    // #flipper-app が表示されるまで待機
    try {
      await viewerPage.waitForSelector('#flipper-app', { state: 'visible', timeout: 15000 });
    } catch (e) {
      console.warn(`    #flipper-app が見つかりません: ${e.message}`);
      await viewerContext.close();
      continue;
    }
    await sleep(2000); // 初期アニメーション完了待機

    // ── Step 2c: ページ数分ループしてスクリーンショット ──
    for (let pageNum = 1; pageNum <= totalPages; pageNum++) {
      const dest = path.join(storeDir, `${flyerName}_p${pageNum}.png`);

      try {
        const flipperEl = await viewerPage.$('#flipper-app');
        if (!flipperEl) { console.warn(`    ページ ${pageNum}: #flipper-app 消失`); break; }

        await flipperEl.screenshot({ path: dest });
        const kb = Math.round(fs.statSync(dest).size / 1024);
        console.log(`    Saved: ${path.basename(dest)} (${kb}KB)`);
        results.push({ store: store.name, flyer: flyerName, page: pageNum, path: dest, url: flipperUrl });

        if (pageNum >= totalPages) break;

        // ── 次ページへ: viewer-flipr-outer ボタンをクリック ──
        const flipRSelectors = ['#viewer-flipr-outer', '#FlipR', '.FlipR', '[data-action="flipR"]', '[id*="flipr"]'];
        let clicked = false;
        for (const sel of flipRSelectors) {
          const btn = await viewerPage.$(sel);
          if (btn) {
            await btn.click();
            clicked = true;
            console.log(`    → 次ページ (${sel})`);
            break;
          }
        }
        if (!clicked) {
          // フォールバック: ページ右端をクリック
          console.warn(`    次ページボタン未検出。右端クリックで代替`);
          await viewerPage.mouse.click(Math.floor(pageW * 0.9), Math.floor(pageH * 0.5));
        }

        await sleep(2000); // めくりアニメーション完了待機

      } catch (e) {
        console.error(`    ページ ${pageNum} 失敗: ${e.message}`);
      }
    }

    await viewerContext.close();
  }

  return results;
}

// ──────────────────────────────────────────────
// 処理方式の自動判別
// ──────────────────────────────────────────────

/**
 * 店舗ページを開いてチラシ取得方式を自動判別する
 * @returns {'tokubai_widget'|'shufoo'|'kamashin_direct'|null}
 */
async function detectType(store, browser) {
  // ドメインで即時判定（ページを開かずに済む）
  if (store.url.includes('24028.jp')) return 'nishimatsuya';

  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
  });
  const page = await context.newPage();

  try {
    await page.goto(store.url, { waitUntil: 'networkidle', timeout: 30000 });
  } catch { /* timeout 継続 */ }
  await sleep(1500);

  // page.evaluate の代わりに page.content() で HTML を取得し Node.js 側で判定する
  // → ページ遷移によって実行コンテキストが破棄されても例外が起きない
  let html = '';
  try {
    html = await page.content();
  } catch (e) {
    console.warn(`  page.content() 失敗: ${e.message}`);
  }

  await context.close();

  if (!html) return null;

  // Tokubai ウィジェットリンクがあれば tokubai_widget
  if (html.includes('widgets.tokubai.co.jp')) return 'tokubai_widget';

  // Shufoo リンクがあれば shufoo
  if (html.includes('shufoo.net') || html.includes('aspViewerRedirect')) return 'shufoo';

  // チラシ画像が直接あれば kamashin_direct
  if (html.includes('alt="チラシ') || html.includes("alt='チラシ")) return 'kamashin_direct';

  // 西松屋フリップブックリンクがあれば nishimatsuya（フォールバック）
  if (html.includes('nishimatsuya.com/flier/')) return 'nishimatsuya';

  return null;
}

// ──────────────────────────────────────────────
// メイン
// ──────────────────────────────────────────────

(async () => {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  const browser    = await chromium.launch({ headless: true });
  const allResults = [];

  for (const store of STORES) {
    // type が設定ファイルに書かれていなければ自動判別
    let type = store.type || null;
    if (!type) {
      console.log(`\n=== ${store.name} — 方式を自動判別中... ===`);
      try {
        type = await detectType(store, browser);
      } catch (e) {
        console.warn(`  判別中にエラー: ${e.message}`);
      }
      if (!type) {
        console.warn(`  判別できませんでした。スキップします。`);
        continue;
      }
      console.log(`  判別結果: ${type}`);
    }

    console.log(`\n=== ${store.name} (${type}) ===`);
    let results = [];

    try {
      if (type === 'tokubai_widget') {
        results = await scrapeTokubaiWidget(store, browser);
      } else if (type === 'tokubai_direct') {
        results = await scrapeTokubaiDirect(store, browser);
      } else if (type === 'shufoo') {
        results = await scrapeShufoo(store, browser);
      } else if (type === 'kamashin_direct') {
        results = await scrapeKamashinDirect(store, browser);
      } else if (type === 'nishimatsuya') {
        results = await scrapeNishimatsuya(store, browser);
      }
    } catch (e) {
      console.error(`  Fatal error: ${e.message}`);
    }

    allResults.push(...results);
    console.log(`  → ${results.length} image(s) saved`);
  }

  await browser.close();

  fs.writeFileSync(MANIFEST_PATH, JSON.stringify(allResults, null, 2), 'utf8');

  console.log('\n=============================');
  console.log(`Done! ${allResults.length} images total`);
  console.log(`Manifest: ${MANIFEST_PATH}`);
  console.log('\nSaved files:');
  allResults.forEach(r => console.log(`  [${r.store}] ${r.path}`));
})();
