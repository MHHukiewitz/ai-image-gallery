import type { AnalysisStatus, ImageOut } from "../lib/api";
import { deltaE, hexToLab, hslToHex } from "./colors";

/** How long an uploaded image stays `pending` before the fake worker picks it up. */
export const PROCESSING_AFTER_MS = 1000;
/** When an uploaded image finishes analyzing. Slow enough that polling visibly does something. */
export const ANALYSIS_DONE_AFTER_MS = 3000;

const SEED_COUNT = 45;
const MINUTE_MS = 60_000;

interface Theme {
  slug: string;
  hue: number;
  tags: string[];
  description: string;
}

const THEMES: Theme[] = [
  {
    slug: "beach",
    hue: 28,
    tags: ["beach", "sunset", "ocean", "sand", "waves", "summer", "coast", "horizon"],
    description: "A golden sunset over a quiet sandy beach.",
  },
  {
    slug: "forest",
    hue: 125,
    tags: ["forest", "trees", "green", "moss", "path", "nature", "fog", "woodland"],
    description: "A misty forest path lined with tall green trees.",
  },
  {
    slug: "city",
    hue: 215,
    tags: ["city", "skyline", "night", "lights", "buildings", "urban", "street", "traffic"],
    description: "A city skyline glowing against the evening sky.",
  },
  {
    slug: "mountain",
    hue: 200,
    tags: ["mountain", "snow", "peak", "hiking", "valley", "alpine", "clouds", "landscape"],
    description: "Snowy mountain peaks rising above a wide valley.",
  },
  {
    slug: "flowers",
    hue: 325,
    tags: ["flowers", "garden", "petals", "pink", "spring", "bloom", "macro", "bouquet"],
    description: "Pink flowers in full bloom in a spring garden.",
  },
  {
    slug: "coffee",
    hue: 25,
    tags: ["coffee", "cup", "table", "morning", "cafe", "drink", "warm", "breakfast"],
    description: "A steaming cup of coffee on a wooden cafe table.",
  },
  {
    slug: "dog",
    hue: 38,
    tags: ["dog", "pet", "puppy", "grass", "playing", "animal", "fur", "outdoors"],
    description: "A happy dog running across a sunny meadow.",
  },
  {
    slug: "architecture",
    hue: 260,
    tags: ["architecture", "building", "glass", "modern", "facade", "lines", "design", "geometry"],
    description: "A modern glass building with sharp geometric lines.",
  },
  {
    slug: "food",
    hue: 12,
    tags: ["food", "pasta", "dinner", "plate", "tomato", "italian", "meal", "fresh"],
    description: "A plate of fresh pasta with tomato sauce and basil.",
  },
  {
    slug: "desert",
    hue: 45,
    tags: ["desert", "dunes", "sand", "dry", "sunrise", "landscape", "warm", "travel"],
    description: "Rolling desert dunes lit by the first morning light.",
  },
];

/** A seeded PRNG, so the mock gallery is identical on every load and tests can rely on it. */
function mulberry32(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface MockImage {
  id: number;
  filename: string;
  uploadedAt: number;
  width: number;
  height: number;
  tags: string[];
  description: string;
  colors: string[];
  /** Set while the fake worker still has work to do; the status is derived from it. */
  analysisStartedAt: number | null;
  /** Names containing "fail" end as `failed`, so the retry UI can be exercised. */
  willFail: boolean;
}

function themeFor(index: number): Theme {
  return THEMES[index % THEMES.length];
}

function paletteFor(theme: Theme, random: () => number): string[] {
  const hue = theme.hue + Math.round(random() * 50) - 25;
  return [
    hslToHex(hue, 55 + random() * 25, 45 + random() * 15),
    hslToHex(hue + 25 + random() * 30, 50 + random() * 30, 30 + random() * 20),
    hslToHex(hue - 30 - random() * 30, 55 + random() * 30, 60 + random() * 20),
  ];
}

function seedImages(now: number): MockImage[] {
  const random = mulberry32(14);
  const sizes: Array<[number, number]> = [
    [1600, 1067],
    [1067, 1600],
    [1400, 1400],
    [1920, 1080],
  ];
  return Array.from({ length: SEED_COUNT }, (_, index) => {
    const id = index + 1;
    const theme = themeFor(index);
    const [width, height] = sizes[index % sizes.length];
    const tagCount = 5 + Math.floor(random() * 4);
    const start = Math.floor(random() * theme.tags.length);
    const tags = Array.from(
      { length: tagCount },
      (_, offset) => theme.tags[(start + offset) % theme.tags.length],
    );
    return {
      id,
      filename: `${theme.slug}-${String(id).padStart(2, "0")}.jpg`,
      // Newest first by id, spread over two days, so the grid has a believable order.
      uploadedAt: now - (SEED_COUNT - id) * 63 * MINUTE_MS,
      width,
      height,
      tags,
      description: theme.description,
      colors: paletteFor(theme, random),
      analysisStartedAt: null,
      willFail: false,
    };
  });
}

function statusOf(image: MockImage, now: number): AnalysisStatus {
  if (image.analysisStartedAt === null) return "done";
  const elapsed = now - image.analysisStartedAt;
  if (elapsed < PROCESSING_AFTER_MS) return "pending";
  if (elapsed < ANALYSIS_DONE_AFTER_MS) return "processing";
  return image.willFail ? "failed" : "done";
}

/** A 300 px square preview drawn as an inline SVG, so the mocks need no image files. */
function svgDataUrl(image: MockImage, width: number, height: number): string {
  const [first, second, third] =
    image.colors.length === 3 ? image.colors : ["#a1a1aa", "#71717a", "#d4d4d8"];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid slice">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${first}"/><stop offset="1" stop-color="${second}"/></linearGradient></defs>
<rect width="${width}" height="${height}" fill="url(#g)"/>
<circle cx="${width * 0.7}" cy="${height * 0.32}" r="${Math.min(width, height) * 0.16}" fill="${third}" opacity="0.9"/>
<path d="M0 ${height} L0 ${height * 0.72} Q${width * 0.3} ${height * 0.5} ${width * 0.55} ${height * 0.72} T${width} ${height * 0.65} L${width} ${height} Z" fill="${second}" opacity="0.65"/>
</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Shapes a record the way the API does. `score` is only passed for search results. */
function toImageOut(
  image: MockImage,
  now: number,
  options: { detail?: boolean; score?: number } = {},
): ImageOut {
  const status = statusOf(image, now);
  const analyzed = status === "done";
  const hasColors = status === "done" || status === "failed";
  return {
    id: image.id,
    filename: image.filename,
    uploaded_at: new Date(image.uploadedAt).toISOString(),
    width: image.width,
    height: image.height,
    thumbnail_url: svgDataUrl(image, 300, 300),
    ...(options.detail && { original_url: svgDataUrl(image, image.width, image.height) }),
    analysis: {
      status,
      tags: analyzed ? image.tags : [],
      description: analyzed ? image.description : null,
      colors: hasColors ? image.colors : [],
      error: status === "failed" ? "The analysis service didn't respond. You can retry." : null,
    },
    ...(options.score !== undefined && { score: options.score }),
  };
}

export type SearchMode =
  | { kind: "all" }
  | { kind: "text"; q: string }
  | { kind: "similar"; imageId: number }
  | { kind: "color"; hex: string };

const SIMILARITY_FLOOR = 0.1;
const MAX_COLOR_DISTANCE = 25;

function tagSimilarity(first: string[], second: string[]): number {
  if (first.length === 0 || second.length === 0) return 0;
  const shared = first.filter((tag) => second.includes(tag)).length;
  return shared / Math.sqrt(first.length * second.length);
}

function colorSimilarity(target: string[], candidate: string[]): number {
  if (target.length === 0 || candidate.length === 0) return 0;
  const candidateLabs = candidate.map(hexToLab);
  const total = target.reduce((sum, hex) => {
    const lab = hexToLab(hex);
    const closest = Math.min(...candidateLabs.map((other) => deltaE(lab, other)));
    return sum + (1 - Math.min(closest, 100) / 100);
  }, 0);
  return total / target.length;
}

/** Prefix match on every word, like the real `:*` tsquery over tags and description. Exact tags weigh most. */
function textScore(image: MockImage, query: string): number {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return 0;
  const prose = image.description.toLowerCase().split(/[^a-z0-9]+/);
  let total = 0;
  for (const word of words) {
    if (image.tags.includes(word)) total += 1;
    else if (image.tags.some((tag) => tag.startsWith(word))) total += 0.8;
    else if (prose.some((token) => token.startsWith(word))) total += 0.4;
    else return 0;
  }
  return total / words.length;
}

export class MockStore {
  private images: MockImage[];
  private nextId = SEED_COUNT + 1;

  constructor(now: number = Date.now()) {
    this.images = seedImages(now);
  }

  /** Newest first, filtered and ranked like the real list endpoint. */
  list(mode: SearchMode, now: number = Date.now()): ImageOut[] {
    const byNewest = [...this.images].sort((a, b) => b.uploadedAt - a.uploadedAt || b.id - a.id);
    const render = (image: MockImage, score?: number) => toImageOut(image, now, { score });

    switch (mode.kind) {
      case "all":
        return byNewest.map((image) => render(image));
      case "text":
        return byNewest
          .map((image) => ({ image, score: textScore(image, mode.q) }))
          .filter(({ score }) => score > 0)
          .sort((a, b) => b.score - a.score)
          .map(({ image, score }) => render(image, score));
      case "similar": {
        const target = this.images.find((image) => image.id === mode.imageId);
        if (!target || statusOf(target, now) !== "done") return [];
        return byNewest
          .filter((image) => image.id !== target.id && statusOf(image, now) === "done")
          .map((image) => ({
            image,
            score:
              0.7 * tagSimilarity(target.tags, image.tags) +
              0.3 * colorSimilarity(target.colors, image.colors),
          }))
          .filter(({ score }) => score > SIMILARITY_FLOOR)
          .sort((a, b) => b.score - a.score)
          .map(({ image, score }) => render(image, score));
      }
      case "color": {
        const query = hexToLab(mode.hex);
        return byNewest
          .map((image) => ({
            image,
            distance: Math.min(
              ...image.colors.map((hex) => deltaE(query, hexToLab(hex))),
              Infinity,
            ),
          }))
          .filter(({ distance }) => distance <= MAX_COLOR_DISTANCE)
          .sort((a, b) => a.distance - b.distance)
          .map(({ image, distance }) => render(image, 1 - distance / MAX_COLOR_DISTANCE));
      }
    }
  }

  get(id: number, now: number = Date.now()): ImageOut | undefined {
    const image = this.images.find((candidate) => candidate.id === id);
    return image && toImageOut(image, now, { detail: true });
  }

  /** Stores a new image as `pending`. The fake worker "finishes" it after a few seconds. */
  add(filename: string, now: number = Date.now()): ImageOut {
    const id = this.nextId++;
    const theme = themeFor(id + 3);
    const random = mulberry32(id * 7919);
    const image: MockImage = {
      id,
      filename,
      uploadedAt: now,
      width: 1600,
      height: 1067,
      tags: theme.tags.slice(0, 6),
      description: theme.description,
      colors: paletteFor(theme, random),
      analysisStartedAt: now,
      willFail: /fail/i.test(filename),
    };
    this.images.push(image);
    return toImageOut(image, now, { detail: true });
  }

  remove(id: number): boolean {
    const before = this.images.length;
    this.images = this.images.filter((image) => image.id !== id);
    return this.images.length < before;
  }

  /** Puts the image back in the queue. A retry always succeeds, even for names containing "fail". */
  reanalyze(id: number, now: number = Date.now()): boolean {
    const image = this.images.find((candidate) => candidate.id === id);
    if (!image) return false;
    image.analysisStartedAt = now;
    image.willFail = false;
    return true;
  }
}
