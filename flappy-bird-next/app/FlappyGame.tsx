'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

type Pipe = { x: number; gapY: number; scored: boolean };
type GameState = 'ready' | 'playing' | 'gameover';
type Snapshot = {
  score: number;
  best: number;
  birdY: number;
  velocity: number;
  state: GameState;
  pipes: number;
  terminalFlaps: number;
  nextPipe?: { x: number; gapY: number };
};

const WIDTH = 430;
const HEIGHT = 640;
const BIRD_X = 110;
const BIRD_RADIUS = 17;
const GRAVITY = 0.34;
const FLAP = -7.2;
const PIPE_WIDTH = 70;
const PIPE_GAP = 168;
const PIPE_DISTANCE = 245;
const PIPE_SPEED = 2.75;
const FLOOR_HEIGHT = 74;

function initialPipes(): Pipe[] {
  return [0, 1, 2].map((i) => ({
    x: WIDTH + 150 + i * PIPE_DISTANCE,
    gapY: 180 + Math.random() * 220,
    scored: false,
  }));
}

export default function FlappyGame() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stateRef = useRef<GameState>('ready');
  const birdYRef = useRef(HEIGHT * 0.42);
  const velocityRef = useRef(0);
  const pipesRef = useRef<Pipe[]>(initialPipes());
  const scoreRef = useRef(0);
  const bestRef = useRef(0);
  const terminalFlapsRef = useRef(0);
  const frameRef = useRef(0);
  const lastTelemetryRef = useRef(0);
  const [snapshot, setSnapshot] = useState<Snapshot>({
    score: 0,
    best: 0,
    birdY: birdYRef.current,
    velocity: 0,
    state: 'ready',
    pipes: pipesRef.current.length,
    terminalFlaps: 0,
  });
  const [events, setEvents] = useState<string[]>(['Waiting for input…']);
  const [bridgeStatus, setBridgeStatus] = useState('connecting');

  const addEvent = useCallback((message: string) => {
    const stamped = `${new Date().toLocaleTimeString()}  ${message}`;
    setEvents((current) => [stamped, ...current].slice(0, 7));
  }, []);

  const publishSnapshot = useCallback(() => {
    const nextPipe = pipesRef.current
      .filter((pipe) => pipe.x + PIPE_WIDTH >= BIRD_X - BIRD_RADIUS)
      .sort((a, b) => a.x - b.x)[0];
    const next = {
      score: scoreRef.current,
      best: bestRef.current,
      birdY: birdYRef.current,
      velocity: velocityRef.current,
      state: stateRef.current,
      pipes: pipesRef.current.length,
      terminalFlaps: terminalFlapsRef.current,
      nextPipe: nextPipe ? { x: nextPipe.x, gapY: nextPipe.gapY } : undefined,
    };
    setSnapshot(next);
    return next;
  }, []);

  const reset = useCallback((autoStart = false) => {
    stateRef.current = autoStart ? 'playing' : 'ready';
    birdYRef.current = HEIGHT * 0.42;
    velocityRef.current = 0;
    pipesRef.current = initialPipes();
    scoreRef.current = 0;
    publishSnapshot();
    addEvent(autoStart ? 'Reset and started.' : 'Reset. Press space/click/terminal flap to start.');
  }, [addEvent, publishSnapshot]);

  const flap = useCallback((source = 'browser') => {
    if (stateRef.current === 'gameover') {
      reset(true);
    } else if (stateRef.current === 'ready') {
      stateRef.current = 'playing';
    }
    velocityRef.current = FLAP;
    if (source === 'terminal') terminalFlapsRef.current += 1;
    addEvent(`${source} flap!`);
    publishSnapshot();
  }, [addEvent, publishSnapshot, reset]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code === 'Space' || event.key === 'ArrowUp') {
        event.preventDefault();
        flap('browser keyboard');
      }
      if (event.key.toLowerCase() === 'r') reset(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [flap, reset]);

  useEffect(() => {
    const source = new EventSource('/events');
    source.addEventListener('open', () => {
      setBridgeStatus('connected');
      addEvent('Terminal bridge connected. Press SPACE/f/ENTER in terminal to flap.');
    });
    source.addEventListener('hello', () => {
      setBridgeStatus('connected');
    });
    source.addEventListener('flap', () => flap('terminal'));
    source.addEventListener('reset', () => reset(false));
    source.addEventListener('error', () => {
      setBridgeStatus('reconnecting');
    });
    return () => source.close();
  }, [addEvent, flap, reset]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const drawBackground = () => {
      const sky = ctx.createLinearGradient(0, 0, 0, HEIGHT);
      sky.addColorStop(0, '#68d8ff');
      sky.addColorStop(0.72, '#b6efff');
      sky.addColorStop(1, '#f8e7a3');
      ctx.fillStyle = sky;
      ctx.fillRect(0, 0, WIDTH, HEIGHT);

      ctx.fillStyle = 'rgba(255,255,255,0.9)';
      for (const cloud of [
        { x: 62, y: 92, s: 1.1 },
        { x: 300, y: 146, s: 0.9 },
        { x: 210, y: 60, s: 0.7 },
      ]) {
        ctx.beginPath();
        ctx.arc(cloud.x, cloud.y, 20 * cloud.s, 0, Math.PI * 2);
        ctx.arc(cloud.x + 24 * cloud.s, cloud.y + 6 * cloud.s, 25 * cloud.s, 0, Math.PI * 2);
        ctx.arc(cloud.x + 52 * cloud.s, cloud.y, 18 * cloud.s, 0, Math.PI * 2);
        ctx.fill();
      }
    };

    const drawPipe = (pipe: Pipe) => {
      const topHeight = pipe.gapY - PIPE_GAP / 2;
      const bottomY = pipe.gapY + PIPE_GAP / 2;
      const bottomHeight = HEIGHT - FLOOR_HEIGHT - bottomY;
      const pipeGradient = ctx.createLinearGradient(pipe.x, 0, pipe.x + PIPE_WIDTH, 0);
      pipeGradient.addColorStop(0, '#168f3b');
      pipeGradient.addColorStop(0.5, '#62d85d');
      pipeGradient.addColorStop(1, '#11742f');
      ctx.fillStyle = pipeGradient;
      ctx.strokeStyle = '#0b5a25';
      ctx.lineWidth = 4;

      ctx.fillRect(pipe.x, 0, PIPE_WIDTH, topHeight);
      ctx.strokeRect(pipe.x, 0, PIPE_WIDTH, topHeight);
      ctx.fillRect(pipe.x - 7, topHeight - 25, PIPE_WIDTH + 14, 25);
      ctx.strokeRect(pipe.x - 7, topHeight - 25, PIPE_WIDTH + 14, 25);

      ctx.fillRect(pipe.x, bottomY, PIPE_WIDTH, bottomHeight);
      ctx.strokeRect(pipe.x, bottomY, PIPE_WIDTH, bottomHeight);
      ctx.fillRect(pipe.x - 7, bottomY, PIPE_WIDTH + 14, 25);
      ctx.strokeRect(pipe.x - 7, bottomY, PIPE_WIDTH + 14, 25);
    };

    const drawBird = () => {
      const y = birdYRef.current;
      ctx.save();
      ctx.translate(BIRD_X, y);
      ctx.rotate(Math.max(-0.55, Math.min(0.75, velocityRef.current / 10)));
      ctx.fillStyle = '#ffd332';
      ctx.strokeStyle = '#a66a00';
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.ellipse(0, 0, 23, 17, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = '#ff9f1a';
      ctx.beginPath();
      ctx.moveTo(18, -2);
      ctx.lineTo(38, 5);
      ctx.lineTo(18, 12);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = '#fff';
      ctx.beginPath();
      ctx.arc(8, -8, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#111827';
      ctx.beginPath();
      ctx.arc(10, -8, 2.4, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#ffe16a';
      ctx.beginPath();
      ctx.ellipse(-9, 7, 11, 6, -0.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    };

    const drawFloor = () => {
      ctx.fillStyle = '#d9a441';
      ctx.fillRect(0, HEIGHT - FLOOR_HEIGHT, WIDTH, FLOOR_HEIGHT);
      ctx.fillStyle = '#7ac943';
      ctx.fillRect(0, HEIGHT - FLOOR_HEIGHT, WIDTH, 18);
      ctx.fillStyle = 'rgba(0,0,0,0.12)';
      for (let x = -((frameRef.current * PIPE_SPEED) % 34); x < WIDTH; x += 34) {
        ctx.fillRect(x, HEIGHT - FLOOR_HEIGHT + 18, 18, 8);
      }
    };

    const drawOverlay = () => {
      ctx.textAlign = 'center';
      ctx.fillStyle = '#ffffff';
      ctx.strokeStyle = 'rgba(0,0,0,0.45)';
      ctx.lineWidth = 5;
      ctx.font = 'bold 54px ui-sans-serif, system-ui';
      ctx.strokeText(String(scoreRef.current), WIDTH / 2, 78);
      ctx.fillText(String(scoreRef.current), WIDTH / 2, 78);

      if (stateRef.current !== 'playing') {
        ctx.fillStyle = 'rgba(5, 16, 40, 0.62)';
        ctx.fillRect(34, 214, WIDTH - 68, 158);
        ctx.fillStyle = '#fff';
        ctx.font = 'bold 30px ui-sans-serif, system-ui';
        ctx.fillText(stateRef.current === 'ready' ? 'Ready?' : 'Game Over', WIDTH / 2, 270);
        ctx.font = '16px ui-sans-serif, system-ui';
        ctx.fillText('Space / click / tap / terminal SPACE to flap', WIDTH / 2, 306);
        ctx.fillText('Press R to reset', WIDTH / 2, 332);
      }
    };

    const collide = () => {
      if (birdYRef.current - BIRD_RADIUS < 0 || birdYRef.current + BIRD_RADIUS > HEIGHT - FLOOR_HEIGHT) {
        return true;
      }
      return pipesRef.current.some((pipe) => {
        const withinX = BIRD_X + BIRD_RADIUS > pipe.x && BIRD_X - BIRD_RADIUS < pipe.x + PIPE_WIDTH;
        const inGap = birdYRef.current - BIRD_RADIUS > pipe.gapY - PIPE_GAP / 2 && birdYRef.current + BIRD_RADIUS < pipe.gapY + PIPE_GAP / 2;
        return withinX && !inGap;
      });
    };

    const tick = (time: number) => {
      frameRef.current += 1;

      if (stateRef.current === 'playing') {
        velocityRef.current += GRAVITY;
        birdYRef.current += velocityRef.current;
        pipesRef.current = pipesRef.current.map((pipe) => ({ ...pipe, x: pipe.x - PIPE_SPEED }));

        const first = pipesRef.current[0];
        if (first && first.x + PIPE_WIDTH < -20) {
          pipesRef.current.shift();
          const lastX = Math.max(...pipesRef.current.map((p) => p.x), WIDTH);
          pipesRef.current.push({ x: lastX + PIPE_DISTANCE, gapY: 155 + Math.random() * 260, scored: false });
        }

        for (const pipe of pipesRef.current) {
          if (!pipe.scored && pipe.x + PIPE_WIDTH < BIRD_X - BIRD_RADIUS) {
            pipe.scored = true;
            scoreRef.current += 1;
            bestRef.current = Math.max(bestRef.current, scoreRef.current);
            addEvent(`Scored! score=${scoreRef.current}`);
          }
        }

        if (collide()) {
          stateRef.current = 'gameover';
          bestRef.current = Math.max(bestRef.current, scoreRef.current);
          addEvent(`Crashed. Final score=${scoreRef.current}`);
        }
      }

      drawBackground();
      pipesRef.current.forEach(drawPipe);
      drawFloor();
      drawBird();
      drawOverlay();

      const telemetryInterval = stateRef.current === 'playing' ? 160 : 1000;
      if (time - lastTelemetryRef.current > telemetryInterval) {
        lastTelemetryRef.current = time;
        const data = publishSnapshot();
        fetch('/api/log', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
          keepalive: true,
        }).catch(() => undefined);
      }

      requestAnimationFrame(tick);
    };

    const animation = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animation);
  }, [addEvent, publishSnapshot]);

  return (
    <main className="page">
      <section className="hero">
        <div>
          <p className="eyebrow">Next.js canvas game</p>
          <h1>Flappy Bird</h1>
          <p className="subtitle">
            A tiny Flappy clone. Control it from the browser, or press <kbd>SPACE</kbd>, <kbd>f</kbd>,
            or <kbd>ENTER</kbd> in the terminal that runs the dev server.
          </p>
        </div>
        <div className={`status ${bridgeStatus}`}>terminal bridge: {bridgeStatus}</div>
      </section>

      <section className="gameGrid">
        <button className="canvasButton" onClick={() => flap('browser click')} aria-label="Flap the bird">
          <canvas ref={canvasRef} width={WIDTH} height={HEIGHT} />
        </button>

        <aside className="panel">
          <h2>Live stats</h2>
          <div className="stats">
            <span>Score</span><strong>{snapshot.score}</strong>
            <span>Best</span><strong>{snapshot.best}</strong>
            <span>State</span><strong>{snapshot.state}</strong>
            <span>Bird Y</span><strong>{Math.round(snapshot.birdY)}</strong>
            <span>Velocity</span><strong>{snapshot.velocity.toFixed(2)}</strong>
            <span>Terminal flaps</span><strong>{snapshot.terminalFlaps}</strong>
          </div>
          <div className="controls">
            <button onClick={() => flap('browser button')}>Flap</button>
            <button onClick={() => reset(false)}>Reset</button>
          </div>
          <h2>Recent events</h2>
          <ul className="events">
            {events.map((event, index) => <li key={`${event}-${index}`}>{event}</li>)}
          </ul>
        </aside>
      </section>
    </main>
  );
}
