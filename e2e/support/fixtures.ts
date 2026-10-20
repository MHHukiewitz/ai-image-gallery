import { fileURLToPath } from "node:url";

export interface Fixture {
  /** The filename the gallery shows, in the modal's title and in the upload queue. */
  file: string;
  path: string;
  /** What FakeAnalyzer says about the image. A finished tile's accessible name is this text. */
  description: string;
}

/**
 * Mirrors `_describe` in api/src/gallery/adapters/analyzers/fake.py: the sentence comes from the
 * image's orientation, its brightness and its dominant hue name. The three fixtures differ in
 * orientation, so each one gets its own sentence and a test can pick a tile by it.
 */
function fixture(file: string, orientation: string, hue: string): Fixture {
  return {
    file,
    path: fileURLToPath(new URL(`../fixtures/${file}`, import.meta.url)),
    description: `A ${orientation} bright picture dominated by ${hue} tones.`,
  };
}

export const RED = fixture("red.png", "square", "red");
/** A different red, in landscape, with a white patch. The nearest neighbor of RED. */
export const RED2 = fixture("red2.png", "landscape", "red");
export const BLUE = fixture("blue.png", "portrait", "blue");

export const ALL_FIXTURES: readonly Fixture[] = [RED, RED2, BLUE];
