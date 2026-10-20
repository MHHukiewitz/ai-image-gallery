// Writes the three PNGs in ../fixtures. Run it with `npm run fixtures`; the output is committed,
// so the suite doesn't need it. Plain RGB images keep every file far under 5 KB, and the sizes
// differ on purpose: the fake analyzer describes an image by its orientation and dominant hue, so
// red.png (square), red2.png (landscape) and blue.png (portrait) each get a different description.
// The suite finds a tile in the grid by that description.
import { deflateSync, crc32 } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures");

const WHITE = [255, 255, 255];

/** One PNG chunk: length, type, data, then a CRC over the type and the data. */
function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const frame = Buffer.alloc(8 + data.length + 4);
  frame.writeUInt32BE(data.length, 0);
  body.copy(frame, 4);
  frame.writeUInt32BE(crc32(body), 8 + data.length);
  return frame;
}

/** Encodes a width x height 8-bit RGB image. `pixelAt(x, y)` returns [r, g, b]. */
function encodePng(width, height, pixelAt) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8; // bit depth
  header[9] = 2; // color type: truecolor RGB

  // Each scanline starts with a filter byte; 0 means the bytes follow unchanged.
  const stride = 1 + width * 3;
  const raw = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const offset = y * stride + 1 + x * 3;
      raw.set(pixelAt(x, y), offset);
    }
  }

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const fixtures = {
  // A square, solid red.
  "red.png": encodePng(64, 64, () => [220, 30, 30]),
  // A landscape, slightly different red with a small white patch. The patch stays far below half
  // of the pixels, so red is still the dominant hue, and it adds white as a second color.
  "red2.png": encodePng(80, 48, (x, y) =>
    x >= 56 && x < 72 && y >= 8 && y < 20 ? WHITE : [200, 50, 50],
  ),
  // A portrait, solid blue.
  "blue.png": encodePng(48, 64, () => [30, 70, 220]),
};

mkdirSync(OUT_DIR, { recursive: true });
for (const [name, bytes] of Object.entries(fixtures)) {
  writeFileSync(join(OUT_DIR, name), bytes);
  console.log(`${name}: ${bytes.length} bytes`);
}
