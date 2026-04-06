/**
 * Registration handlers: device auth flow, Turnstile verification, PoW validation.
 *
 * Implements the OAuth-style device code flow for anonymous API key registration:
 * 1. CLI calls /register/init → gets device_code + user_code
 * 2. User opens browser, enters code, solves Turnstile + PoW
 * 3. CLI polls /register/poll/:device_code until key is ready
 */

import { jsonResponse, htmlResponse, generateHex, generateUserCode, hashIpForRateLimit, incrementMetric } from "./helpers.js";

// --- Registration constants ---

const DEVICE_CODE_TTL = 600; // 10 minutes
const KEY_COOLDOWN_SECONDS = 3600; // 1 hour before new key can upload
// Configurable via env for testing (production defaults hardcoded here)
let REG_RATE_LIMIT_PER_DAY = 3; // per IP
let REG_INIT_RATE_LIMIT_PER_MINUTE = 10; // per IP, prevents /register/init flooding
const TURNSTILE_VERIFY_URL =
  "https://challenges.cloudflare.com/turnstile/v0/siteverify";

// --- Proof-of-Work ---
// Client must find a nonce where SHA-256(challenge + nonce) starts with
// POW_DIFFICULTY zero hex chars. ~5s on typical hardware at difficulty 5.
const POW_DIFFICULTY = 5;
const POW_CHALLENGE_TTL = 300; // 5 minutes

export { KEY_COOLDOWN_SECONDS };

// --- Proof-of-Work helpers ---

function generatePowChallenge() {
  return generateHex(16); // 32-char random challenge
}

async function verifyPow(challenge, nonce, difficulty = POW_DIFFICULTY) {
  const data = new TextEncoder().encode(challenge + nonce);
  const hash = await crypto.subtle.digest("SHA-256", data);
  const hex = [...new Uint8Array(hash)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return hex.startsWith("0".repeat(difficulty));
}

// --- Registration handlers ---

export async function handleRegisterInit(request, env) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const date = new Date().toISOString().slice(0, 10);
  const ipHash = await hashIpForRateLimit(ip, date);

  // Per-IP per-minute rate limit on init endpoint (prevents flooding)
  const initRateMax = parseInt(env.REG_INIT_RATE_LIMIT_PER_MINUTE || REG_INIT_RATE_LIMIT_PER_MINUTE, 10);
  const minute = new Date().toISOString().slice(0, 16);
  const initRateKey = `reg_init:${ipHash}:${minute}`;
  const initCount = parseInt(
    (await env.METRICS.get(initRateKey)) || "0",
    10
  );
  if (initCount >= initRateMax) {
    return jsonResponse(
      { error: "Too many requests. Please wait a moment." },
      429
    );
  }
  await env.METRICS.put(initRateKey, String(initCount + 1), {
    expirationTtl: 120,
  });

  // Per-IP registration rate limit (transient, not stored permanently)
  const regRateMax = parseInt(env.REG_RATE_LIMIT_PER_DAY || REG_RATE_LIMIT_PER_DAY, 10);
  const regRateKey = `reg_rate:${ipHash}:${date}`;
  const regCount = parseInt((await env.METRICS.get(regRateKey)) || "0", 10);
  if (regCount >= regRateMax) {
    await incrementMetric(env, "registrations_rate_limited");
    return jsonResponse(
      { error: "Registration rate limit exceeded. Try again tomorrow." },
      429
    );
  }
  await env.METRICS.put(regRateKey, String(regCount + 1), {
    expirationTtl: 86400,
  });

  // Generate device code and user code
  const deviceCode = generateHex(16); // 32 hex chars
  const userCode = generateUserCode();
  const userCodeNorm = userCode.replace("-", "");

  // Generate proof-of-work challenge
  const powChallenge = generatePowChallenge();

  // Store in METRICS KV with TTL
  await env.METRICS.put(
    `device:${deviceCode}`,
    JSON.stringify({
      user_code: userCodeNorm,
      status: "pending",
      pow_challenge: powChallenge,
      created_at: new Date().toISOString(),
    }),
    { expirationTtl: DEVICE_CODE_TTL }
  );
  await env.METRICS.put(`usercode:${userCodeNorm}`, deviceCode, {
    expirationTtl: DEVICE_CODE_TTL,
  });
  // Store PoW challenge separately so the browser page can fetch it
  await env.METRICS.put(`pow:${userCodeNorm}`, powChallenge, {
    expirationTtl: POW_CHALLENGE_TTL,
  });

  const baseUrl = new URL(request.url).origin;
  return jsonResponse({
    device_code: deviceCode,
    user_code: userCode,
    verification_url: `${baseUrl}/register`,
    expires_in: DEVICE_CODE_TTL,
    poll_interval: 5,
  });
}

export function handleRegisterPage(env) {
  const siteKey = env.TURNSTILE_SITE_KEY || "";
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Common Parlance - Register</title>
  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
  <style>
    body{background:#1a1a2e;color:#e0e0e0;font-family:system-ui,-apple-system,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
    .card{background:#16213e;border-radius:12px;padding:2rem;max-width:400px;width:90%;box-shadow:0 4px 24px rgba(0,0,0,0.3)}
    h1{font-size:1.4rem;margin-top:0;color:#a8b2d1}
    p{color:#8892b0;font-size:0.95rem;line-height:1.5}
    label{display:block;color:#8892b0;font-size:0.85rem;margin-bottom:0.4rem}
    input{width:100%;padding:0.75rem;font-size:1.5rem;text-align:center;letter-spacing:0.3em;text-transform:uppercase;background:#0f3460;border:1px solid #533483;border-radius:8px;color:#e0e0e0;box-sizing:border-box;margin-bottom:1rem;font-family:monospace}
    input:focus{outline:none;border-color:#a8b2d1}
    button{width:100%;padding:0.75rem;font-size:1rem;background:#533483;color:white;border:none;border-radius:8px;cursor:pointer;font-weight:600}
    button:hover{background:#6a4c9c}
    button:disabled{opacity:0.5;cursor:not-allowed}
    .msg{margin-top:1rem;padding:0.75rem;border-radius:8px;font-size:0.9rem;display:none}
    .msg.ok{display:block;background:#1b4332;color:#95d5b2}
    .msg.err{display:block;background:#3d0000;color:#ff6b6b}
    .cf-turnstile{margin-bottom:1rem;display:flex;justify-content:center}
    .footer{margin-top:1.5rem;text-align:center;color:#4a5568;font-size:0.75rem}
    .footer a{color:#533483}
    @media(prefers-color-scheme:light){
      body{background:#f5f5f5;color:#1a1a2e}
      .card{background:#fff;box-shadow:0 4px 24px rgba(0,0,0,0.1)}
      h1{color:#333}
      p{color:#555}
      label{color:#555}
      input{background:#f0f0f0;border-color:#ccc;color:#1a1a2e}
      input:focus{border-color:#533483}
      .msg.ok{background:#d4edda;color:#155724}
      .msg.err{background:#f8d7da;color:#721c24}
      .footer{color:#999}
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>Common Parlance</h1>
    <p>Enter the code from your terminal to register for an anonymous API key.</p>
    <label for="code">Verification Code (letters only)</label>
    <input id="code" maxlength="9" placeholder="ABCD-EFGH" autocomplete="off" autofocus aria-label="Verification code from your terminal">
    <div class="cf-turnstile" data-sitekey="${siteKey}"></div>
    <button id="submit" type="button" onclick="register()">Register</button>
    <div id="msg" role="status" aria-live="polite"></div>
    <div class="footer">
      No account needed. Registration is anonymous.<br>
      <a href="https://github.com/common-parlance/common-parlance/blob/main/PRIVACY.md">Privacy policy</a>
    </div>
  </div>
  <script>
    const codeInput = document.getElementById('code');
    codeInput.addEventListener('input', function(e) {
      let v = e.target.value.replace(/[^A-Za-z]/g, '').toUpperCase();
      if (v.length > 4) v = v.slice(0,4) + '-' + v.slice(4,8);
      e.target.value = v;
    });

    // Proof-of-Work solver: find nonce where SHA-256(challenge+nonce) starts with N zeros
    async function solvePoW(challenge, difficulty) {
      const prefix = '0'.repeat(difficulty);
      let nonce = 0;
      while (true) {
        const data = new TextEncoder().encode(challenge + nonce);
        const hash = await crypto.subtle.digest('SHA-256', data);
        const hex = [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2,'0')).join('');
        if (hex.startsWith(prefix)) return nonce;
        nonce++;
        // Yield to UI every 1000 iterations to keep page responsive
        if (nonce % 1000 === 0) await new Promise(r => setTimeout(r, 0));
      }
    }

    async function register() {
      const code = codeInput.value.trim();
      if (code.replace('-','').length !== 8) {
        showMsg('Please enter the 8-character code from your terminal.', 'err');
        return;
      }
      const turnstileEl = document.querySelector('[name="cf-turnstile-response"]');
      if (!turnstileEl || !turnstileEl.value) {
        showMsg('Please complete the verification challenge. If it does not appear, try reloading the page.', 'err');
        return;
      }
      const btn = document.getElementById('submit');
      btn.disabled = true;
      btn.textContent = 'Verifying (may take a few seconds)...';
      try {
        // Fetch PoW challenge for this user code
        const userCode = code.replace('-','').toUpperCase();
        const challengeResp = await fetch('/register/challenge/' + userCode);
        if (!challengeResp.ok) {
          const err = await challengeResp.json();
          showMsg(err.error || 'Code expired or invalid.', 'err');
          btn.disabled = false;
          btn.textContent = 'Register';
          return;
        }
        const { challenge, difficulty } = await challengeResp.json();

        // Solve proof-of-work (takes a few seconds)
        const nonce = await solvePoW(challenge, difficulty);

        btn.textContent = 'Registering...';
        const resp = await fetch('/register/complete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            user_code: userCode,
            turnstile_token: turnstileEl.value,
            pow_nonce: nonce
          })
        });
        const data = await resp.json();
        if (resp.ok) {
          showMsg('Registration complete! You can close this tab and return to your terminal.', 'ok');
          btn.style.display = 'none';
          codeInput.disabled = true;
        } else {
          showMsg(data.error || 'Registration failed. Please try again.', 'err');
          btn.disabled = false;
          btn.textContent = 'Register';
          if (typeof turnstile !== 'undefined') turnstile.reset();
        }
      } catch(e) {
        showMsg('Network error. Please try again.', 'err');
        btn.disabled = false;
        btn.textContent = 'Register';
      }
    }
    function showMsg(text, type) {
      const el = document.getElementById('msg');
      el.textContent = text;
      el.className = 'msg ' + type;
    }
    codeInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') register();
    });
  </script>
</body>
</html>`;
  return htmlResponse(html);
}

export async function handleRegisterComplete(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ error: "Invalid request body" }, 400);
  }

  const { user_code, turnstile_token, pow_nonce } = body;
  if (!user_code || !turnstile_token) {
    return jsonResponse(
      { error: "Missing user_code or turnstile_token" },
      400
    );
  }
  if (pow_nonce == null) {
    return jsonResponse({ error: "Missing proof-of-work solution" }, 400);
  }

  // Validate Turnstile token
  let turnstileResult;
  try {
    const turnstileUrl = env.TURNSTILE_VERIFY_URL || TURNSTILE_VERIFY_URL;
    const turnstileResp = await fetch(turnstileUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        secret: env.TURNSTILE_SECRET || "",
        response: turnstile_token,
        remoteip: request.headers.get("CF-Connecting-IP") || "",
      }),
    });
    turnstileResult = await turnstileResp.json();
  } catch (err) {
    console.error(`Turnstile verification error: ${err.message}`);
    return jsonResponse({ error: "Verification service unavailable. Please try again." }, 503);
  }
  if (!turnstileResult.success) {
    await incrementMetric(env, "turnstile_failures_total");
    return jsonResponse({ error: "Verification challenge failed. Please reload the page and try again." }, 403);
  }

  // Look up user code
  const userCodeNorm = user_code.replace("-", "").toUpperCase();

  // Verify proof-of-work
  const powChallenge = await env.METRICS.get(`pow:${userCodeNorm}`);
  if (!powChallenge) {
    return jsonResponse({ error: "PoW challenge expired or invalid" }, 400);
  }
  const powValid = await verifyPow(powChallenge, String(pow_nonce));
  if (!powValid) {
    await incrementMetric(env, "pow_failures_total");
    return jsonResponse({ error: "Invalid proof-of-work solution" }, 400);
  }
  // Clean up PoW challenge (one-time use)
  await env.METRICS.delete(`pow:${userCodeNorm}`);
  const deviceCode = await env.METRICS.get(`usercode:${userCodeNorm}`);
  if (!deviceCode) {
    return jsonResponse({ error: "Code expired or invalid" }, 404);
  }

  // Check device code status
  const deviceData = await env.METRICS.get(`device:${deviceCode}`);
  if (!deviceData) {
    return jsonResponse({ error: "Code expired" }, 404);
  }
  let device;
  try {
    device = JSON.parse(deviceData);
  } catch {
    console.error("Corrupt device data for code");
    return jsonResponse({ error: "Registration error. Please try again." }, 500);
  }
  if (device.status === "complete") {
    return jsonResponse({ error: "Already registered" }, 409);
  }

  // Generate API key with prefix for secret scanning
  const apiKey = "cp_live_" + generateHex(16);

  // Store key in API_KEYS
  await env.API_KEYS.put(
    apiKey,
    JSON.stringify({
      created_at: new Date().toISOString(),
      tier: 1,
      tier_updated: new Date().toISOString(),
    })
  );

  // Mark device code as complete (CLI will poll this)
  await env.METRICS.put(
    `device:${deviceCode}`,
    JSON.stringify({ status: "complete", api_key: apiKey }),
    { expirationTtl: DEVICE_CODE_TTL }
  );

  // Clean up user code
  await env.METRICS.delete(`usercode:${userCodeNorm}`);

  await incrementMetric(env, "registrations_total");
  console.log("Registration complete: new key issued");

  return jsonResponse({ ok: true });
}

export async function handleRegisterPoll(deviceCode, env) {
  const deviceData = await env.METRICS.get(`device:${deviceCode}`);
  if (!deviceData) {
    return jsonResponse({ error: "Unknown or expired device code" }, 404);
  }

  let device;
  try {
    device = JSON.parse(deviceData);
  } catch {
    return jsonResponse({ error: "Registration error" }, 500);
  }
  if (device.status === "pending") {
    return jsonResponse({ status: "pending" });
  }

  if (device.status === "complete") {
    // Delete after successful retrieval (one-time use)
    await env.METRICS.delete(`device:${deviceCode}`);
    return jsonResponse({ status: "complete", api_key: device.api_key });
  }

  return jsonResponse({ status: "pending" });
}

// --- PoW challenge endpoint ---

export async function handlePowChallenge(userCode, env) {
  const userCodeNorm = userCode.toUpperCase();
  const challenge = await env.METRICS.get(`pow:${userCodeNorm}`);
  if (!challenge) {
    return jsonResponse({ error: "Code expired or invalid" }, 404);
  }
  return jsonResponse({ challenge, difficulty: POW_DIFFICULTY });
}

// --- Public contribution stats ---

export async function handlePublicStats(env) {
  if (!env.METRICS) return jsonResponse({ error: "Not configured" }, 503);

  // Aggregate stats — no per-key breakdown, fully anonymous
  const stats = {
    total_contributions: parseInt(
      (await env.METRICS.get("conversations_total")) || "0",
      10
    ),
    total_uploads: parseInt(
      (await env.METRICS.get("uploads_total")) || "0",
      10
    ),
    total_contributors: parseInt(
      (await env.METRICS.get("registrations_total")) || "0",
      10
    ),
    content_blocks: parseInt(
      (await env.METRICS.get("content_blocks_total")) || "0",
      10
    ),
    pii_rejections: parseInt(
      (await env.METRICS.get("pii_rejections_total")) || "0",
      10
    ),
    dedup_blocks: parseInt(
      (await env.METRICS.get("dedup_blocks_total")) || "0",
      10
    ),
  };

  return jsonResponse(stats, 200, {
    "Cache-Control": "public, max-age=300",
  });
}
