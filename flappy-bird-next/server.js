const http = require('http');
const next = require('next');

const dev = process.env.NODE_ENV !== 'production';
const hostname = 'localhost';
const port = Number(process.env.PORT || 3000);
const app = next({ dev, hostname, port });
const handle = app.getRequestHandler();

const clients = new Set();
let flapCount = 0;
let logCount = 0;

function now() {
  return new Date().toLocaleTimeString();
}

function sendSse(res, event, data) {
  res.write(`event: ${event}\n`);
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

function broadcast(event, data) {
  for (const res of clients) {
    sendSse(res, event, data);
  }
}

function printControls() {
  console.log('');
  console.log('🐦 Flappy Bird Next.js is running!');
  console.log(`➡  Open http://${hostname}:${port}`);
  console.log('⌨️  Terminal controls: SPACE / f / ENTER = flap, r = reset, a = autopilot, q = quit');
  console.log('🧪 Browser controls: SPACE / click / tap = flap');
  console.log('📡 The browser sends game telemetry back here every couple seconds.');
  console.log('');
}

function setupTerminalControls(server) {
  if (!process.stdin.isTTY) {
    console.log('[terminal] stdin is not a TTY, terminal controls disabled.');
    return;
  }

  process.stdin.setRawMode(true);
  process.stdin.resume();
  process.stdin.setEncoding('utf8');

  process.stdin.on('data', (input) => {
    for (const key of input) {
      if (key === '\u0003' || key.toLowerCase() === 'q') {
        console.log(`\n[${now()}] shutting down...`);
        process.stdin.setRawMode(false);
        server.close(() => process.exit(0));
        setTimeout(() => process.exit(0), 500).unref();
        return;
      }

      if (key === ' ' || key === '\r' || key === '\n' || key.toLowerCase() === 'f') {
        terminalFlap('manual');
        continue;
      }

      if (key.toLowerCase() === 'r') {
        console.log(`[${now()}] terminal RESET -> sent to ${clients.size} browser client(s)`);
        broadcast('reset', { at: Date.now(), source: 'terminal' });
      }

      if (key.toLowerCase() === 'a') {
        autopilot = !autopilot;
        console.log(`[${now()}] 🤖 terminal autopilot ${autopilot ? 'ON' : 'OFF'} (${clients.size} client(s) connected)`);
        if (autopilot) broadcast('reset', { at: Date.now(), source: 'terminal autopilot' });
      }
    }
  });
}

app.prepare().then(() => {
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url || '/', `http://${req.headers.host || `${hostname}:${port}`}`);

      if (url.pathname === '/events') {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache, no-transform',
          Connection: 'keep-alive',
          'X-Accel-Buffering': 'no',
        });
        sendSse(res, 'hello', { message: 'connected to Flappy terminal bridge', at: Date.now() });
        clients.add(res);
        console.log(`[${now()}] browser connected to terminal bridge (${clients.size} client(s))`);
        req.on('close', () => {
          clients.delete(res);
          console.log(`[${now()}] browser disconnected from terminal bridge (${clients.size} client(s))`);
        });
        return;
      }

      if (url.pathname === '/api/log' && req.method === 'POST') {
        let body = '';
        req.on('data', (chunk) => { body += chunk; });
        req.on('end', () => {
          try {
            const data = JSON.parse(body || '{}');
            logCount += 1;
            const score = Number(data.score ?? 0);
            const state = data.state ?? 'unknown';
            const next = data.nextPipe ? `, nextPipe={x:${Math.round(data.nextPipe.x)}, gapY:${Math.round(data.nextPipe.gapY)}}` : '';
            if (autopilot || logCount % 6 === 1 || score !== lastLoggedScore || state !== lastLoggedState) {
              console.log(
                `[${now()}] game telemetry #${logCount}: ` +
                `score=${score}, y=${Math.round(data.birdY ?? 0)}, ` +
                `velocity=${Number(data.velocity ?? 0).toFixed(2)}, state=${state}, ` +
                `pipes=${data.pipes ?? 0}${next}`
              );
            }
            lastLoggedScore = score;
            lastLoggedState = state;
            runAutopilot(data);
            res.writeHead(204);
            res.end();
          } catch (error) {
            console.log(`[${now()}] bad telemetry payload: ${error.message}`);
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ok: false }));
          }
        });
        return;
      }

      await handle(req, res);
    } catch (err) {
      console.error('[server] request error', err);
      res.statusCode = 500;
      res.end('internal server error');
    }
  });

  server.listen(port, hostname, () => {
    printControls();
    setupTerminalControls(server);
  });
});
function terminalFlap(reason = 'manual') {
  flapCount += 1;
  const payload = { id: flapCount, at: Date.now(), source: reason === 'autopilot' ? 'terminal autopilot' : 'terminal' };
  console.log(`[${now()}] terminal FLAP #${flapCount}${reason === 'autopilot' ? ' 🤖' : ''} -> sent to ${clients.size} browser client(s)`);
  broadcast('flap', payload);
}

function runAutopilot(data) {
  if (!autopilot || clients.size === 0) return;

  const state = data.state || 'unknown';
  if (state === 'gameover') {
    console.log(`[${now()}] 🤖 autopilot saw gameover -> reset + restart`);
    broadcast('reset', { at: Date.now(), source: 'terminal autopilot' });
    setTimeout(() => terminalFlap('autopilot'), 120);
    return;
  }

  if (state === 'ready') {
    terminalFlap('autopilot');
    return;
  }

  if (state !== 'playing') return;

  const y = Number(data.birdY || 0);
  const velocity = Number(data.velocity || 0);
  const nextPipe = data.nextPipe || {};
  const gapY = Number(nextPipe.gapY || 300);
  const pipeX = Number(nextPipe.x || 999);
  const nowMs = Date.now();

  // Aim near the gap centre. Only flap while low, or falling fast while close to target.
  const targetY = gapY + (pipeX < 260 ? 8 : -8);
  const shouldFlap = y > targetY + 10 || (y > targetY - 22 && velocity > 6.2);
  if (shouldFlap && nowMs - lastAutoFlapAt > 190) {
    lastAutoFlapAt = nowMs;
    console.log(`[${now()}] 🤖 autopilot decision: y=${Math.round(y)}, target=${Math.round(targetY)}, v=${velocity.toFixed(2)}, nextPipeX=${Math.round(pipeX)}`);
    terminalFlap('autopilot');
  }
}
let autopilot = false;
let lastAutoFlapAt = 0;
let lastLoggedScore = 0;
let lastLoggedState = 'ready';
let lastAutoResetAt = 0;
    if (Date.now() - lastAutoResetAt < 900) return;
    lastAutoResetAt = Date.now();
