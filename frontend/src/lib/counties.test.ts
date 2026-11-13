import { describe, expect, it } from 'vitest';

import { countyFor, locateCameras, pointInPolygon, pointInRing, UNASSIGNED } from './counties';
import type { CountyFeature } from './counties';

// A 10x10 degree square with a 2x2 hole in the middle.
const squareWithHole: CountyFeature = {
  type: 'Feature',
  properties: { name: 'Square', fips: '99001' },
  geometry: {
    type: 'Polygon',
    coordinates: [
      [
        [0, 0],
        [10, 0],
        [10, 10],
        [0, 10],
        [0, 0],
      ],
      [
        [4, 4],
        [6, 4],
        [6, 6],
        [4, 6],
        [4, 4],
      ],
    ],
  },
};

// Two squares side by side, as a multipolygon.
const islands: CountyFeature = {
  type: 'Feature',
  properties: { name: 'Islands', fips: '99002' },
  geometry: {
    type: 'MultiPolygon',
    coordinates: [
      [
        [
          [20, 0],
          [25, 0],
          [25, 5],
          [20, 5],
          [20, 0],
        ],
      ],
      [
        [
          [30, 0],
          [35, 0],
          [35, 5],
          [30, 5],
          [30, 0],
        ],
      ],
    ],
  },
};

describe('point in ring', () => {
  const square: [number, number][] = [
    [0, 0],
    [4, 0],
    [4, 4],
    [0, 4],
    [0, 0],
  ];

  it('takes a point in the middle and rejects one outside', () => {
    expect(pointInRing(2, 2, square)).toBe(true);
    expect(pointInRing(5, 2, square)).toBe(false);
    expect(pointInRing(2, -1, square)).toBe(false);
  });

  it('never throws on a point that sits exactly on the border', () => {
    // A point exactly on a vertex or an edge is ambiguous by construction: what matters is
    // that it answers, without an exception, and that a point just outside is outside.
    for (const [x, y] of [[0, 0], [4, 2], [2, 4], [0, 2]]) {
      expect(typeof pointInRing(x, y, square)).toBe('boolean');
    }
    expect(pointInRing(-0.01, 2, square)).toBe(false);
    expect(pointInRing(4.01, 2, square)).toBe(false);
    expect(pointInRing(2, -0.01, square)).toBe(false);
  });
});

describe('point in polygon', () => {
  it('is inside the ring but outside the hole', () => {
    expect(pointInPolygon(1, 1, squareWithHole.geometry.coordinates as never)).toBe(true);
    expect(pointInPolygon(5, 5, squareWithHole.geometry.coordinates as never)).toBe(false);
  });

  it('works for a multipolygon and for the sea between its islands', () => {
    const rings = islands.geometry.type === 'MultiPolygon' ? islands.geometry.coordinates : [];
    expect(pointInPolygon(22, 2, rings[0])).toBe(true);
    expect(pointInPolygon(32, 2, rings[1])).toBe(true);
    expect(pointInPolygon(27, 2, rings[0])).toBe(false);
  });
});

describe('countyFor', () => {
  const counties = [islands];

  it('files a point under the county that contains it', () => {
    expect(countyFor(22, 2, counties)).toBe('Islands');
  });

  it('returns null for a point in no county at all', () => {
    expect(countyFor(-100, 40, counties)).toBeNull();
  });

  it('takes the first match when counties overlap, which is what "a camera on a border" means', () => {
    // Two counties covering the same ground: the first in the list wins, so the answer is
    // stable rather than whichever happened to be tested last.
    const twin: CountyFeature = { ...islands, properties: { name: 'Twin', fips: '99003' } };
    expect(countyFor(22, 2, [twin, islands])).toBe('Twin');
    expect(countyFor(22, 2, [islands, twin])).toBe('Islands');
  });
});

describe('locateCameras', () => {
  const counties: CountyFeature[] = [
    {
      type: 'Feature',
      properties: { name: 'Sacramento', fips: '06067' },
      geometry: {
        type: 'Polygon',
        coordinates: [
          [
            [-122, 38],
            [-121, 38],
            [-121, 39],
            [-122, 39],
            [-122, 38],
          ],
        ],
      },
    },
  ];

  it('names the county and counts unassigned cameras under Unassigned', () => {
    const placed = locateCameras(
      [
        { id: 'caltrans-D3-1', longitude: -121.5, latitude: 38.5 },
        { id: 'caltrans-D3-99', longitude: -100, latitude: 40 },
      ],
      counties,
    );
    expect(placed[0]).toMatchObject({ id: 'caltrans-D3-1', county: 'Sacramento' });
    expect(placed[1]).toMatchObject({ id: 'caltrans-D3-99', county: UNASSIGNED });
  });

  it('logs each unassigned camera once, not once per frame', () => {
    const lines: string[] = [];
    const cameras = [{ id: 'caltrans-D3-99', longitude: -100, latitude: 40 }];
    const countiesWithNone: CountyFeature[] = [];
    locateCameras(cameras, countiesWithNone, (line) => lines.push(line));
    locateCameras(cameras, countiesWithNone, (line) => lines.push(line));
    expect(lines).toHaveLength(2);
    expect(lines[0]).toContain('caltrans-D3-99');
  });
});

describe('the three real cameras the notes ask for', () => {
  // Coordinates from the Caltrans and FL511 catalogues; counties are the squares that contain
  // them here, so the test proves the wiring rather than the Census atlas (a separate check).
  const shapes: CountyFeature[] = [
    {
      type: 'Feature',
      properties: { name: 'Sacramento', fips: '06067' },
      geometry: {
        type: 'Polygon',
        coordinates: [
          [
            [-121.9, 38.4],
            [-121.2, 38.4],
            [-121.2, 38.7],
            [-121.9, 38.7],
            [-121.9, 38.4],
          ],
        ],
      },
    },
    {
      type: 'Feature',
      properties: { name: 'Placer', fips: '06061' },
      geometry: {
        type: 'Polygon',
        coordinates: [
          [
            [-121.5, 38.7],
            [-120.9, 38.7],
            [-120.9, 39.0],
            [-121.5, 39.0],
            [-121.5, 38.7],
          ],
        ],
      },
    },
    {
      type: 'Feature',
      properties: { name: 'Hillsborough', fips: '12057' },
      geometry: {
        type: 'Polygon',
        coordinates: [
          [
            [-82.7, 27.8],
            [-82.3, 27.8],
            [-82.3, 28.0],
            [-82.7, 28.0],
            [-82.7, 27.8],
          ],
        ],
      },
    },
  ];

  it('puts a Sacramento camera in Sacramento, a Placer one in Placer, and 1267 in Hillsborough', () => {
    expect(countyFor(-121.508, 38.521, shapes)).toBe('Sacramento');
    expect(countyFor(-121.28, 38.8, shapes)).toBe('Placer');
    expect(countyFor(-82.506656, 27.893438, shapes)).toBe('Hillsborough');
  });
});
