/** Reading `pipeline/track_count.py --results-json` output in the browser.
 *
 *  Shape (see `write_results_json` in the pipeline):
 *    boxes: [[frame_index, [[track_id, class_index, conf_percent, x1, y1, x2, y2], ...]], ...]
 *  `boxes_every` may be more than 1, so a requested frame uses the nearest
 *  earlier entry rather than an exact match.
 */

export const RESULTS_FORMAT = 'traffic-analyzer-results/1';

export type BoxRow = [number, number, number, number, number, number, number];

export type LaneDef = {
  id: number;
  polygon: [number, number][];
  line: [[number, number], [number, number]];
  directions?: Record<string, string>;
};

export type ResultEvent = {
  timestamp: string;
  frame: number;
  track_id: number;
  class: string | null;
  lane: number;
  direction: string;
};

export type ResultsFile = {
  format: string;
  video?: string;
  fps: number;
  frame_size: [number, number];
  frames_total?: number;
  start?: string;
  model?: string;
  tracker?: string;
  conf?: number;
  classes: string[];
  lanes: { frame_size?: [number, number]; lanes: LaneDef[] };
  events: ResultEvent[];
  track_classes: Record<string, string>;
  boxes_every: number;
  boxes: [number, BoxRow[]][];
};

const fail = (message: string): never => {
  throw new Error(message);
};

function isNumberTuple(value: unknown, length: number): value is number[] {
  return (
    Array.isArray(value) &&
    value.length === length &&
    value.every((item) => typeof item === 'number' && Number.isFinite(item))
  );
}

/** Parse and validate a results file. Throws an Error with a plain message. */
export function parseResults(text: string): ResultsFile {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    fail('That file is not JSON.');
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    fail('That file is not a results object.');
  }

  const candidate = data as Partial<ResultsFile>;
  if (candidate.format !== RESULTS_FORMAT) {
    fail(
      `Unsupported results format ${JSON.stringify(candidate.format ?? null)}; expected ${RESULTS_FORMAT}.`,
    );
  }
  if (typeof candidate.fps !== 'number' || !Number.isFinite(candidate.fps) || candidate.fps <= 0) {
    fail('The results file has no usable fps.');
  }
  if (!isNumberTuple(candidate.frame_size, 2)) fail('The results file has no frame_size [width, height].');
  if (!candidate.lanes || !Array.isArray(candidate.lanes.lanes)) {
    fail('The results file has no lanes.');
  }
  if (!Array.isArray(candidate.events)) fail('The results file has no events array.');
  if (!Array.isArray(candidate.boxes)) fail('The results file has no boxes array.');
  if (typeof candidate.boxes_every !== 'number' || candidate.boxes_every < 1) {
    fail('The results file has no usable boxes_every.');
  }
  if (!Array.isArray(candidate.classes)) fail('The results file has no classes array.');

  return candidate as ResultsFile;
}

/** Frame index for a playback position: floor(currentTime * fps). */
export function frameIndexAt(timeSeconds: number, fps: number): number {
  if (!Number.isFinite(timeSeconds) || timeSeconds <= 0) return 0;
  return Math.floor(timeSeconds * fps);
}

/** Boxes for the nearest entry at or before `frame`; empty when none is earlier. */
export function boxesAtFrame(boxes: [number, BoxRow[]][], frame: number): BoxRow[] {
  if (!boxes.length) return [];
  let low = 0;
  let high = boxes.length - 1;
  let found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (boxes[mid][0] <= frame) {
      found = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return found === -1 ? [] : boxes[found][1];
}

/** Counts per lane and direction for events already passed at `frame`. */
export function countsAtFrame(events: ResultEvent[], frame: number): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const event of events) {
    if (event.frame > frame) continue;
    const key = `${event.lane} ${event.direction}`;
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}
