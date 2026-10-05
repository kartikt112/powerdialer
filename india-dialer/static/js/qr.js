/* QR codes, generated locally: no CDN, no network.
   Byte mode, versions 1-10, error correction L / M / Q / H, automatic mask
   choice by the standard penalty score. Enough for a `tel:` link and a LAN
   URL. The structure follows ISO/IEC 18004 as laid out in Nayuki's
   reference implementation; tests/test_qr.py checks every module against
   the segno library for all eight masks. */

const ECC_PER_BLOCK = {
  L: [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18],
  M: [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26],
  Q: [-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24],
  H: [-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28]
};
const NUM_BLOCKS = {
  L: [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4],
  M: [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5],
  Q: [-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8],
  H: [-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8]
};
const FORMAT_BITS = { L: 1, M: 0, Q: 3, H: 2 };
const MAX_VERSION = 10;

function rawDataModules(ver) {
  let result = (16 * ver + 128) * ver + 64;
  if (ver >= 2) {
    const numAlign = Math.floor(ver / 7) + 2;
    result -= (25 * numAlign - 10) * numAlign - 55;
    if (ver >= 7) result -= 36;
  }
  return result;
}
const dataCodewords = (ver, ecl) => Math.floor(rawDataModules(ver) / 8) - ECC_PER_BLOCK[ecl][ver] * NUM_BLOCKS[ecl][ver];

/* ---- Reed-Solomon over GF(256), polynomial 0x11D ---------------------- */

function gfMul(x, y) {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z;
}
function rsDivisor(degree) {
  const result = new Array(degree).fill(0);
  result[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < result.length; j++) {
      result[j] = gfMul(result[j], root);
      if (j + 1 < result.length) result[j] ^= result[j + 1];
    }
    root = gfMul(root, 0x02);
  }
  return result;
}
function rsRemainder(data, divisor) {
  const result = divisor.map(() => 0);
  for (const b of data) {
    const factor = b ^ result.shift();
    result.push(0);
    divisor.forEach((coef, i) => { result[i] ^= gfMul(coef, factor); });
  }
  return result;
}

/* ---- data codewords ------------------------------------------------------ */

function utf8(text) { return Array.from(new TextEncoder().encode(String(text))); }

function encodeData(bytes, ver, ecl) {
  const bits = [];
  const push = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
  push(0x4, 4);                                   // byte mode
  push(bytes.length, ver <= 9 ? 8 : 16);
  bytes.forEach((b) => push(b, 8));
  const capacity = dataCodewords(ver, ecl) * 8;
  push(0, Math.min(4, capacity - bits.length));   // terminator
  push(0, (8 - (bits.length % 8)) % 8);
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) push(pad, 8);
  const out = [];
  for (let i = 0; i < bits.length; i += 8) out.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
  return out;
}

function addEccAndInterleave(data, ver, ecl) {
  const numBlocks = NUM_BLOCKS[ecl][ver], blockEcc = ECC_PER_BLOCK[ecl][ver];
  const raw = Math.floor(rawDataModules(ver) / 8);
  const numShort = numBlocks - (raw % numBlocks), shortLen = Math.floor(raw / numBlocks);
  const divisor = rsDivisor(blockEcc);
  const blocks = [];
  for (let i = 0, k = 0; i < numBlocks; i++) {
    const dat = data.slice(k, k + shortLen - blockEcc + (i < numShort ? 0 : 1));
    k += dat.length;
    const ecc = rsRemainder(dat, divisor);
    if (i < numShort) dat.push(0);                // placeholder, skipped below
    blocks.push(dat.concat(ecc));
  }
  const result = [];
  for (let i = 0; i < blocks[0].length; i++) {
    blocks.forEach((block, j) => {
      if (i !== shortLen - blockEcc || j >= numShort) result.push(block[i]);
    });
  }
  return result;
}

/* ---- the symbol ----------------------------------------------------------- */

function alignmentPositions(ver, size) {
  if (ver === 1) return [];
  const numAlign = Math.floor(ver / 7) + 2;
  const step = Math.ceil((ver * 4 + 4) / (numAlign * 2 - 2)) * 2;
  const result = [6];
  for (let pos = size - 7; result.length < numAlign; pos -= step) result.splice(1, 0, pos);
  return result;
}

function build(ver, ecl, codewords, forcedMask) {
  const size = ver * 4 + 17;
  const modules = Array.from({ length: size }, () => new Array(size).fill(false));
  const isFn = Array.from({ length: size }, () => new Array(size).fill(false));
  const setFn = (x, y, dark) => { modules[y][x] = dark; isFn[y][x] = true; };
  const bit = (x, i) => ((x >>> i) & 1) !== 0;

  for (let i = 0; i < size; i++) { setFn(6, i, i % 2 === 0); setFn(i, 6, i % 2 === 0); }
  const finder = (x, y) => {
    for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
      const d = Math.max(Math.abs(dx), Math.abs(dy)), xx = x + dx, yy = y + dy;
      if (xx >= 0 && xx < size && yy >= 0 && yy < size) setFn(xx, yy, d !== 2 && d !== 4);
    }
  };
  finder(3, 3); finder(size - 4, 3); finder(3, size - 4);
  const align = alignmentPositions(ver, size), n = align.length;
  for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
    if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) continue;
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) setFn(align[i] + dx, align[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
  }
  const drawFormat = (mask) => {
    const data = (FORMAT_BITS[ecl] << 3) | mask;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    for (let i = 0; i <= 5; i++) setFn(8, i, bit(bits, i));
    setFn(8, 7, bit(bits, 6)); setFn(8, 8, bit(bits, 7)); setFn(7, 8, bit(bits, 8));
    for (let i = 9; i < 15; i++) setFn(14 - i, 8, bit(bits, i));
    for (let i = 0; i < 8; i++) setFn(size - 1 - i, 8, bit(bits, i));
    for (let i = 8; i < 15; i++) setFn(8, size - 15 + i, bit(bits, i));
    setFn(8, size - 8, true);
  };
  drawFormat(0);
  if (ver >= 7) {
    let rem = ver;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const bits = (ver << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const a = size - 11 + (i % 3), b = Math.floor(i / 3);
      setFn(a, b, bit(bits, i)); setFn(b, a, bit(bits, i));
    }
  }
  // codewords in the zigzag
  let i = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert++) for (let j = 0; j < 2; j++) {
      const x = right - j, upward = ((right + 1) & 2) === 0, y = upward ? size - 1 - vert : vert;
      if (!isFn[y][x] && i < codewords.length * 8) { modules[y][x] = bit(codewords[i >>> 3], 7 - (i & 7)); i++; }
    }
  }
  const MASKS = [
    (x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
    (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
    (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0, (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0
  ];
  const applyMask = (m) => {
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!isFn[y][x] && MASKS[m](x, y)) modules[y][x] = !modules[y][x];
  };
  let mask = forcedMask;
  if (mask == null || mask < 0) {
    let best = Infinity;
    for (let m = 0; m < 8; m++) {
      applyMask(m); drawFormat(m);
      const p = penalty(modules, size);
      if (p < best) { best = p; mask = m; }
      applyMask(m);
    }
  }
  applyMask(mask); drawFormat(mask);
  return { size, modules, mask, version: ver, ecl };
}

function penalty(m, size) {
  let result = 0;
  const addHistory = (len, h) => { if (h[0] === 0) len += size; h.pop(); h.unshift(len); };
  const count = (h) => {
    const n = h[1], core = n > 0 && h[2] === n && h[3] === n * 3 && h[4] === n && h[5] === n;
    return (core && h[0] >= n * 4 && h[6] >= n ? 1 : 0) + (core && h[6] >= n * 4 && h[0] >= n ? 1 : 0);
  };
  const terminate = (color, len, h) => { if (color) { addHistory(len, h); len = 0; } len += size; addHistory(len, h); return count(h); };
  for (let pass = 0; pass < 2; pass++) {
    for (let a = 0; a < size; a++) {
      let color = false, run = 0;
      const h = [0, 0, 0, 0, 0, 0, 0];
      for (let b = 0; b < size; b++) {
        const v = pass === 0 ? m[a][b] : m[b][a];
        if (v === color) { run++; if (run === 5) result += 3; else if (run > 5) result++; }
        else { addHistory(run, h); if (!color) result += count(h) * 40; color = v; run = 1; }
      }
      result += terminate(color, run, h) * 40;
    }
  }
  for (let y = 0; y < size - 1; y++) for (let x = 0; x < size - 1; x++) {
    const c = m[y][x];
    if (c === m[y][x + 1] && c === m[y + 1][x] && c === m[y + 1][x + 1]) result += 3;
  }
  let dark = 0;
  m.forEach((row) => row.forEach((v) => { if (v) dark++; }));
  const total = size * size;
  return result + (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
}

/* ---- public ----------------------------------------------------------------- */

/* {size, modules[y][x], mask, version, ecl}. opts: ecl ('M'), version (auto), mask (auto). */
export function qrMatrix(text, opts) {
  opts = opts || {};
  const ecl = opts.ecl || "M", bytes = utf8(text);
  let ver = opts.version || 0;
  const fits = (v) => 4 + (v <= 9 ? 8 : 16) + bytes.length * 8 <= dataCodewords(v, ecl) * 8;
  if (ver && !fits(ver)) throw new Error("Text does not fit a version " + ver + "-" + ecl + " QR code");
  if (!ver) {
    for (let v = 1; v <= MAX_VERSION; v++) {
      if (fits(v)) { ver = v; break; }
    }
    if (!ver) throw new Error("Text too long for a local QR code");
  }
  const codewords = addEccAndInterleave(encodeData(bytes, ver, ecl), ver, ecl);
  return build(ver, ecl, codewords, opts.mask);
}

/* A crisp SVG: one path, dark modules only, a 4-module quiet zone. */
export function qrSvg(text, opts) {
  opts = opts || {};
  const q = qrMatrix(text, opts), border = opts.border == null ? 4 : opts.border, dim = q.size + border * 2;
  let d = "";
  for (let y = 0; y < q.size; y++) for (let x = 0; x < q.size; x++) if (q.modules[y][x]) d += "M" + (x + border) + " " + (y + border) + "h1v1h-1z";
  return '<svg class="qr" viewBox="0 0 ' + dim + " " + dim + '" shape-rendering="crispEdges" role="img" aria-label="' +
    (opts.label || "QR code") + '"><rect width="' + dim + '" height="' + dim + '" fill="#fff"/><path d="' + d + '" fill="#000"/></svg>';
}
