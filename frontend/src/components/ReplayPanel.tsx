import { useCallback, useEffect, useRef, useState } from 'react';
import { boxesAtFrame, countsAtFrame, frameIndexAt, parseResults, type ResultsFile } from '../lib/results';

/** Replay a local video with the pipeline's boxes, lanes and counts drawn on it.
 *  Both files come from the user's disk, so nothing is uploaded or hosted. */

export function ReplayPanel() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const frameRef = useRef<HTMLSpanElement | null>(null);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [videoName, setVideoName] = useState('');
  const [results, setResults] = useState<ResultsFile | null>(null);
  const [resultsName, setResultsName] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      if (videoUrl) URL.revokeObjectURL(videoUrl);
    };
  }, [videoUrl]);

  const onVideo = (file: File | undefined) => {
    if (!file) return;
    setVideoUrl((current) => {
      if (current) URL.revokeObjectURL(current);
      return URL.createObjectURL(file);
    });
    setVideoName(file.name);
  };

  const onResults = async (file: File | undefined) => {
    if (!file) return;
    setError(null);
    setResultsName(file.name);
    try {
      const parsed = parseResults(await file.text());
      setResults(parsed);
    } catch (cause) {
      setResults(null);
      setError(cause instanceof Error ? cause.message : 'That results file could not be read.');
    }
  };

  const draw = useCallback(() => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas || !results) return;

    const rect = video.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.round(rect.width * dpr);
    const height = Math.round(rect.height * dpr);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }

    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, width, height);

    const [frameWidth, frameHeight] = results.frame_size;
    const scaleX = width / frameWidth;
    const scaleY = height / frameHeight;
    const font = `${Math.round(12 * dpr)}px ui-sans-serif, system-ui, sans-serif`;

    const frame = frameIndexAt(video.currentTime, results.fps);
    if (frameRef.current) frameRef.current.textContent = String(frame);

    // Lanes are stored in the lanes file's own frame_size, which can differ from the video.
    const laneFrame = results.lanes.frame_size ?? results.frame_size;
    const laneScaleX = (frameWidth / laneFrame[0]) * scaleX;
    const laneScaleY = (frameHeight / laneFrame[1]) * scaleY;
    const counts = countsAtFrame(results.events, frame);

    // Labels are placed above each lane's counting line; one that would overlap a
    // label already drawn is nudged up, and dropped if there is still no room.
    const drawnLabels: [number, number, number, number][] = [];
    const labelDraws: { text: string; x: number; y: number; w: number; h: number; textX: number }[] = [];
    const overlaps = (rect: [number, number, number, number]) =>
      drawnLabels.some(
        (other) => !(rect[2] <= other[0] || rect[0] >= other[2] || rect[3] <= other[1] || rect[1] >= other[3]),
      );

    ctx.lineJoin = 'round';
    for (const lane of results.lanes.lanes ?? []) {
      const points = lane.polygon ?? [];
      if (points.length > 2) {
        ctx.beginPath();
        points.forEach(([x, y], index) => {
          const px = x * laneScaleX;
          const py = y * laneScaleY;
          if (index === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        });
        ctx.closePath();
        ctx.fillStyle = 'rgba(20, 83, 75, 0.18)';
        ctx.fill();
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
        ctx.lineWidth = Math.max(1, 2 * dpr);
        ctx.stroke();
      }

      if (lane.line && lane.line.length === 2) {
        const [[x1, y1], [x2, y2]] = lane.line;
        ctx.beginPath();
        ctx.setLineDash([6 * dpr, 4 * dpr]);
        ctx.moveTo(x1 * laneScaleX, y1 * laneScaleY);
        ctx.lineTo(x2 * laneScaleX, y2 * laneScaleY);
        ctx.strokeStyle = '#ffd166';
        ctx.lineWidth = Math.max(1, 2 * dpr);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      const centroid = lane.polygon?.length
        ? lane.polygon.reduce(
            (acc, [x, y]) => [acc[0] + x / lane.polygon.length, acc[1] + y / lane.polygon.length],
            [0, 0] as [number, number],
          )
        : null;
      const lineMiddle =
        lane.line && lane.line.length === 2
          ? ([
              (lane.line[0][0] + lane.line[1][0]) / 2,
              (lane.line[0][1] + lane.line[1][1]) / 2,
            ] as [number, number])
          : null;
      const anchor = lineMiddle ?? centroid;
      if (anchor) {
        const label = `Lane ${lane.id}`;
        const parts = Object.entries(counts)
          .filter(([key]) => key.startsWith(`${lane.id} `))
          .map(([key, value]) => `${key.split(' ')[1]} ${value}`);
        const text = parts.length ? `${label} · ${parts.join(' · ')}` : label;
        ctx.font = font;
        const padding = 4 * dpr;
        const boxHeight = 16 * dpr;
        const boxWidth = ctx.measureText(text).width + padding * 2;
        const wantedX = Math.min(
          Math.max(anchor[0] * laneScaleX - boxWidth / 2, 2 * dpr),
          Math.max(2 * dpr, width - boxWidth - 2 * dpr),
        );
        const above = anchor[1] * laneScaleY - boxHeight - 4 * dpr;
        const offsets = [0, -(boxHeight + 3 * dpr), -2 * (boxHeight + 3 * dpr), boxHeight + 3 * dpr];
        for (const offset of offsets) {
          const rect: [number, number, number, number] = [
            wantedX,
            above + offset,
            wantedX + boxWidth,
            above + offset + boxHeight,
          ];
          if (overlaps(rect)) continue;
          drawnLabels.push(rect);
          // Drawn after the vehicle boxes so a detection label cannot cover the counts.
          labelDraws.push({
            text,
            x: wantedX,
            y: above + offset,
            w: boxWidth,
            h: boxHeight,
            textX: wantedX + padding,
          });
          break;
        }
      }
    }

    for (const [trackId, classIndex, confidence, x1, y1, x2, y2] of boxesAtFrame(results.boxes, frame)) {
      const left = x1 * scaleX;
      const top = y1 * scaleY;
      const boxWidth = (x2 - x1) * scaleX;
      const boxHeight = (y2 - y1) * scaleY;
      ctx.strokeStyle = '#7fe7c4';
      ctx.lineWidth = Math.max(1, 2 * dpr);
      ctx.strokeRect(left, top, boxWidth, boxHeight);

      const className = results.classes[classIndex] ?? `class ${classIndex}`;
      const label = `${trackId} ${className} ${Math.round(confidence)}%`;
      ctx.font = font;
      const textWidth = ctx.measureText(label).width;
      const labelTop = Math.max(0, top - 16 * dpr);
      ctx.fillStyle = '#7fe7c4';
      ctx.fillRect(left, labelTop, textWidth + 6 * dpr, 16 * dpr);
      ctx.fillStyle = '#06251f';
      ctx.fillText(label, left + 3 * dpr, labelTop + 12 * dpr);
    }

    // Lane counts last, so a detection label never hides them.
    ctx.font = font;
    for (const item of labelDraws) {
      ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
      ctx.fillRect(item.x, item.y, item.w, item.h);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(item.text, item.textX, item.y + 11 * dpr);
    }
  }, [results]);

  useEffect(() => {
    if (!results || !videoUrl) return;
    let raf = 0;
    const tick = () => {
      draw();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [draw, results, videoUrl]);

  const lanes = results?.lanes.lanes ?? [];

  return (
    <section className="card" aria-labelledby="replay-heading">
      <header>
        <div>
          <h2 id="replay-heading">Replay</h2>
          <p className="note" style={{ margin: 0 }}>
            Pick a processed video and its <code>_results.json</code>. Both stay on your machine;
            nothing is uploaded.
          </p>
        </div>
        {results ? (
          <p className="note" style={{ margin: 0 }}>
            {results.fps.toFixed(2)} fps · frame <span ref={frameRef}>0</span> · every{' '}
            {results.boxes_every} frame{results.boxes_every === 1 ? '' : 's'} · {lanes.length} lanes
          </p>
        ) : null}
      </header>

      <div className="replay-files">
        <div className="field">
          <label htmlFor="replay-video">Video file</label>
          <input
            id="replay-video"
            type="file"
            accept="video/*,.mp4,.ts,.mov,.mkv"
            onChange={(event) => onVideo(event.target.files?.[0])}
          />
        </div>
        <div className="field">
          <label htmlFor="replay-results">Results JSON</label>
          <input
            id="replay-results"
            type="file"
            accept="application/json,.json"
            onChange={(event) => void onResults(event.target.files?.[0])}
          />
        </div>
      </div>

      {error ? (
        <div className="state error" role="alert">
          <strong>That results file was rejected.</strong>
          <div>{error}</div>
        </div>
      ) : null}

      {videoUrl ? (
        <div className="replay-stage">
          <video ref={videoRef} src={videoUrl} controls playsInline preload="metadata" onSeeked={draw} />
          <canvas ref={canvasRef} aria-hidden="true" />
        </div>
      ) : (
        <div className="state">Choose a video to start the replay. The overlay appears once a results file is loaded.</div>
      )}

      <div className="replay-legend">
        <span>{videoName || 'No video loaded'}</span>
        <span>{resultsName || 'No results loaded'}</span>
        {results ? <span>{results.video ? `recorded as ${results.video}` : ''}</span> : null}
        {results ? <span>{results.events.length.toLocaleString()} counted crossings</span> : null}
      </div>
    </section>
  );
}
