const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function sanitize(text) {
  return String(text ?? "")
    .replace(/access_token=[^&\s"']+/g, "access_token=<redacted>")
    .replace(
      /eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g,
      "<redacted-jwt>",
    );
}

function setStatus(state, text) {
  statusEl.dataset.state = state;
  statusEl.textContent = text;
}

function setResult(result) {
  window.__lkResult = result;
  resultEl.textContent = sanitize(JSON.stringify(result));
}

let SDK = null;
let sdkSource = null;
let sdkError = null;
try {
  SDK = await import("/vendor/livekit-client.esm.mjs");
  sdkSource = "local";
} catch (localError) {
  const cdnUrl = window.__lkSdkCdnUrl;
  if (cdnUrl) {
    try {
      SDK = await import(cdnUrl);
      sdkSource = "cdn";
    } catch (cdnError) {
      sdkError = `${localError}; ${cdnError}`;
    }
  } else {
    sdkError = String(localError);
  }
}
window.__lkStartReady = true;

function makeCanvasTrack() {
  const canvas = document.createElement("canvas");
  canvas.width = 160;
  canvas.height = 120;
  const ctx = canvas.getContext("2d");
  let frame = 0;
  const timer = setInterval(() => {
    ctx.fillStyle = `hsl(${(frame++ * 23) % 360}, 80%, 50%)`;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }, 100);
  const stream = canvas.captureStream(10);
  const track = stream.getVideoTracks()[0];
  track.addEventListener("ended", () => clearInterval(timer));
  return track;
}

function transceiverDirections(pc) {
  try {
    return pc.getTransceivers().map((transceiver) => transceiver.direction);
  } catch (err) {
    return [];
  }
}

function classifyPc(pc, room) {
  const manager = room && room.engine && room.engine.pcManager;
  const pub = manager && manager.publisher && manager.publisher._pc;
  const sub = manager && manager.subscriber && manager.subscriber._pc;
  if (pub && sub && pub === pc && sub === pc) {
    return { label: "single", association: "sdk" };
  }
  if (pub === pc) {
    const directions = transceiverDirections(pc);
    const receives = directions.includes("recvonly") ||
      directions.includes("sendrecv");
    return {
      label: receives ? "single" : "publisher",
      association: "sdk",
    };
  }
  if (sub === pc) {
    const directions = transceiverDirections(pc);
    const sends = directions.includes("sendonly") ||
      directions.includes("sendrecv");
    return {
      label: sends ? "single" : "subscriber",
      association: "sdk",
    };
  }
  const directions = transceiverDirections(pc);
  const sends = directions.includes("sendonly") ||
    directions.includes("sendrecv");
  const receives = directions.includes("recvonly") ||
    directions.includes("sendrecv");
  if (sends && receives) {
    return { label: "single", association: "transceiver" };
  }
  if (sends) {
    return { label: "publisher", association: "transceiver" };
  }
  if (receives) {
    return { label: "subscriber", association: "transceiver" };
  }
  return { label: "pc", association: "transceiver" };
}

function hasSelectedPair(stats) {
  const byId = new Map(stats.map((stat) => [stat.id, stat]));
  const transport = stats.find(
    (stat) => stat.type === "transport" && stat.selectedCandidatePairId,
  );
  if (transport) {
    const pair = byId.get(transport.selectedCandidatePairId);
    return Boolean(pair && pair.state === "succeeded");
  }
  return stats.some(
    (stat) => stat.type === "candidate-pair" && stat.state === "succeeded" &&
      (stat.selected === true || stat.nominated === true),
  );
}

async function collectPc(pc, room) {
  const classified = classifyPc(pc, room);
  const report = await pc.getStats();
  const stats = [];
  report.forEach((stat) => {
    const copy = {};
    for (const key of Object.keys(stat)) {
      copy[key] = stat[key];
    }
    stats.push(copy);
  });
  return {
    label: classified.label,
    association: classified.association,
    directions: transceiverDirections(pc),
    connectionState: pc.connectionState,
    iceConnectionState: pc.iceConnectionState,
    iceGatheringState: pc.iceGatheringState,
    signalingState: pc.signalingState,
    stats,
  };
}

window.__lkStart = async (config) => {
  const result = {
    ok: false,
    errorCode: null,
    error: null,
    sdk: { version: config.sdkVersion || null, source: sdkSource },
    pcs: [],
  };
  setResult(result);

  if (!SDK) {
    result.errorCode = "sdk_unavailable";
    result.error = sanitize(
      sdkError || "livekit-client could not be loaded",
    );
    setResult(result);
    setStatus("failed", "sdk unavailable");
    return;
  }

  let room = null;
  let connectedOnce = false;
  let timer = null;
  try {
    setStatus("connecting", "connecting");
    room = new SDK.Room();
    const connected = new Promise((resolve, reject) => {
      timer = setTimeout(
        () => reject(new Error("connect timeout")),
        config.timeoutMs || 15000,
      );
      room.on(SDK.RoomEvent.Connected, () => {
        clearTimeout(timer);
        resolve();
      });
    });
    await room.connect(config.url, config.token, {
      autoSubscribe: true,
      maxRetries: 0,
      websocketTimeout: config.websocketTimeoutMs || 15000,
    });
    await connected;
    connectedOnce = true;
    setStatus("connected", "connected");

    setStatus("publishing", "publishing");
    const track = makeCanvasTrack();
    await room.localParticipant.publishTrack(track, {
      name: "connectivity-check-canvas",
      source: SDK.Track.Source.Camera,
      simulcast: false,
    });

    const deadline = Date.now() + (config.statsTimeoutMs || 20000);
    let payloads = [];
    while (true) {
      const peers = (window.__lkPeers || []).filter(
        (pc) => pc.connectionState !== "closed",
      );
      payloads = [];
      for (const pc of peers) {
        payloads.push(await collectPc(pc, room));
      }
      const complete = payloads.length > 0 &&
        payloads.every((pc) => hasSelectedPair(pc.stats));
      if (complete || Date.now() >= deadline) {
        break;
      }
      await sleep(200);
    }

    if (payloads.some((pc) => hasSelectedPair(pc.stats))) {
      // Let gathering settle so relay candidates that finish just after the
      // selected pair still appear in the "gathered" report.
      await sleep(500);
      const peers = (window.__lkPeers || []).filter(
        (pc) => pc.connectionState !== "closed",
      );
      payloads = [];
      for (const pc of peers) {
        payloads.push(await collectPc(pc, room));
      }
    }

    result.pcs = payloads;
    result.ok = payloads.length > 0 && payloads.every(
      (pc) => pc.connectionState === "connected" &&
        hasSelectedPair(pc.stats),
    );
    setResult(result);
    setStatus(
      result.ok ? "ready" : "failed",
      result.ok ? "ready" : "no selected pair",
    );
  } catch (err) {
    if (timer) {
      clearTimeout(timer);
    }
    result.errorCode = connectedOnce ? "publish_failed" : "join_failed";
    result.error = sanitize(err && err.message ? err.message : err);
    setResult(result);
    setStatus("failed", "failed");
  }
};
