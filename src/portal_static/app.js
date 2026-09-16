const state = {
  branchId: "shellharbour",
  branches: [],
  diary: { slots: [], bookings: [] },
  room: null,
  connecting: false,
  deskSource: null,
  seenDeskIds: new Set(),
  groundingViolations: 0,
};

const DESK_TOPIC = "ava.desk";

const FIELD_LABELS = {
  name: "Name",
  time: "Time",
  date: "Date",
  doctor: "Doctor",
  branch: "Branch",
  reason: "Reason",
  booking_id: "Booking",
  phone: "Phone",
  open_slots: "Open slots",
  matches: "Matches",
  failure_reason: "Failure",
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    credentials: "same-origin",
    ...options,
  });
  const text = await response.text();
  let body = {};
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { detail: text };
    }
  }
  if (!response.ok) {
    const error = new Error(
      typeof body.detail === "string"
        ? body.detail
        : body.detail?.note || body.detail?.reason || response.statusText
    );
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

function todayISO() {
  const now = new Date();
  const tz = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - tz).toISOString().slice(0, 10);
}

function renderTabs() {
  const nav = $("branch-tabs");
  nav.innerHTML = "";
  for (const branch of state.branches) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = branch.trading_name;
    if (branch.id === state.branchId) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => {
      state.branchId = branch.id;
      renderTabs();
      renderFacts();
      loadDiary();
    });
    nav.appendChild(button);
  }
}

function currentBranch() {
  return state.branches.find((item) => item.id === state.branchId) || state.branches[0];
}

function renderFacts() {
  const branch = currentBranch();
  if (!branch) return;
  $("fact-suburb").textContent = branch.suburb;
  $("fact-name").textContent = branch.trading_name;
  $("fact-address").textContent = branch.address;
  $("fact-phone").textContent = branch.phone;
  $("fact-hours").textContent = branch.hours;
  $("fact-parking").textContent = branch.parking;
  $("fact-languages").textContent = branch.languages;
  $("fact-cancellation").textContent = branch.cancellation;
  const list = $("fact-dentists");
  list.innerHTML = "";
  const clinicians = branch.clinicians?.length
    ? branch.clinicians
    : (branch.dentists || []).map((name) => ({ name }));
  for (const clinician of clinicians) {
    const item = document.createElement("li");
    const extra = [clinician.role, clinician.ahpra].filter(Boolean).join(" · ");
    item.innerHTML = `<strong>${clinician.name}</strong>${
      extra ? `<div class="role">${extra}</div>` : ""
    }`;
    list.appendChild(item);
  }
}

function bookingForSlot(slotId) {
  return (state.diary.bookings || []).find((item) => item.slot_id === slotId);
}

function renderDiary() {
  const date = $("diary-date").value;
  const slots = (state.diary.slots || []).filter((slot) => slot.date === date);
  $("diary-empty").classList.toggle("hidden", slots.length > 0);
  const list = $("slot-list");
  list.innerHTML = "";
  for (const slot of slots) {
    const booking = slot.taken ? bookingForSlot(slot.slot_id) : null;
    const row = document.createElement("article");
    row.className = `slot ${slot.taken ? "taken" : "open"}`;
    const who = booking
      ? `${booking.patient_name} · ${booking.reason}`
      : "Open";
    row.innerHTML = `
      <time>${slot.time}</time>
      <div class="meta">
        <div>${slot.clinician}</div>
        <div class="who">${who}</div>
      </div>
      <div class="actions"></div>
    `;
    const actions = row.querySelector(".actions");
    if (!slot.taken) {
      const book = document.createElement("button");
      book.type = "button";
      book.textContent = "Book";
      book.addEventListener("click", () => openBook(slot));
      actions.appendChild(book);
    } else if (booking) {
      const move = document.createElement("button");
      move.type = "button";
      move.className = "ghost";
      move.textContent = "Reschedule";
      move.addEventListener("click", () => openReschedule(booking));
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "ghost";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => cancelBooking(booking.booking_id));
      actions.append(move, cancel);
    }
    list.appendChild(row);
  }
}

async function loadDiary() {
  const date = $("diary-date").value;
  state.diary = await api(
    `/api/diary?branch_id=${encodeURIComponent(state.branchId)}&date_from=${date}&date_to=${date}`
  );
  renderDiary();
}

function openBook(slot, booking = null) {
  $("book-title").textContent = booking ? "Reschedule appointment" : "Book appointment";
  $("book-slot-label").textContent = `${slot.date} ${slot.time} with ${slot.clinician}`;
  $("book-slot-id").value = slot.slot_id;
  $("book-booking-id").value = booking?.booking_id || "";
  $("book-name").value = booking?.patient_name || "";
  $("book-phone").value = booking?.patient_phone || "";
  $("book-reason").value = booking?.reason || "Check-up";
  $("book-error").classList.add("hidden");
  $("book-dialog").showModal();
}

function openReschedule(booking) {
  const open = (state.diary.slots || []).find((slot) => !slot.taken);
  if (!open) {
    alert("No open slots on this date. Pick another day first.");
    return;
  }
  openBook(open, booking);
}

async function cancelBooking(bookingId) {
  if (!confirm("Cancel this appointment?")) return;
  const result = await api(`/api/bookings/${bookingId}/cancel`, { method: "POST" });
  if (result.confirmed) await loadDiary();
}

$("book-cancel").addEventListener("click", () => $("book-dialog").close());

$("book-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const slotId = $("book-slot-id").value;
  const bookingId = $("book-booking-id").value;
  $("book-error").classList.add("hidden");
  try {
    let result;
    if (bookingId) {
      result = await api(`/api/bookings/${bookingId}/reschedule`, {
        method: "POST",
        body: JSON.stringify({ new_slot_id: slotId }),
      });
    } else {
      result = await api("/api/bookings", {
        method: "POST",
        body: JSON.stringify({
          branch_id: state.branchId,
          slot_id: slotId,
          name: $("book-name").value,
          phone: $("book-phone").value,
          reason: $("book-reason").value,
        }),
      });
    }
    if (!result.confirmed) throw new Error("Not confirmed");
    $("book-dialog").close();
    await loadDiary();
  } catch (error) {
    $("book-error").textContent = error.message || "Booking failed";
    $("book-error").classList.remove("hidden");
  }
});

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function appendLiveLine(containerId, className, html) {
  const list = $(containerId);
  const line = document.createElement("article");
  line.className = className;
  line.innerHTML = html;
  list.appendChild(line);
  list.scrollTop = list.scrollHeight;
}

function renderTranscript(packet) {
  const role = packet.role === "assistant" ? "assistant" : "user";
  const who = role === "assistant" ? "Ava" : "Caller";
  const room = packet.room ? ` · ${packet.room}` : "";
  appendLiveLine(
    "live-transcript",
    `live-line ${role}`,
    `<div class="who">${who}${escapeHtml(room)}</div><div>${escapeHtml(packet.text || "")}</div>`
  );
}

function renderActivity(packet) {
  const payload = packet.payload || {};
  const fields = Object.entries(FIELD_LABELS)
    .filter(([key]) => payload[key] !== undefined && payload[key] !== "")
    .map(
      ([key, label]) =>
        `<span><strong>${escapeHtml(label)}:</strong> ${escapeHtml(payload[key])}</span>`
    )
    .join("");
  const room = packet.room ? ` · ${packet.room}` : "";
  appendLiveLine(
    "live-activity",
    "live-line activity",
    `<div class="who">${escapeHtml(
      packet.label || packet.action || "Activity"
    )}${escapeHtml(room)}</div>${fields ? `<div class="fields">${fields}</div>` : ""}`
  );
}

async function maybeRefreshDiary(packet) {
  if (!packet.refresh_diary) return;
  const payload = packet.payload || {};
  if (payload.date && $("diary-date").value !== payload.date) {
    $("diary-date").value = payload.date;
  }
  if (payload.branch && payload.branch !== state.branchId) {
    state.branchId = payload.branch;
    renderTabs();
    renderFacts();
  }
  await loadDiary();
}

function renderGrounding(packet) {
  const count = Number(packet.payload?.count || state.groundingViolations + 1);
  state.groundingViolations = count;
  const el = $("grounding-count");
  if (el) el.textContent = `Grounding violations: ${count}`;
  appendLiveLine(
    "live-activity",
    "live-line activity",
    `<div class="who">GROUNDING_VIOLATION</div><div class="fields">${escapeHtml(
      (packet.payload?.violations || []).join(", ")
    )}</div>`
  );
}

function handleDeskPacket(packet) {
  if (!packet || typeof packet !== "object") return;
  if (packet.id) {
    if (state.seenDeskIds.has(packet.id)) return;
    state.seenDeskIds.add(packet.id);
  }
  if (packet.room) {
    const hint = $("live-call-hint");
    const channel = packet.channel === "sip" ? "Inbound phone" : "Live call";
    hint.textContent = `${channel} · ${packet.room}`;
  }
  if (packet.type === "grounding_violation") {
    renderGrounding(packet);
    return;
  }
  if (packet.type === "transcript" && packet.text) {
    renderTranscript(packet);
    return;
  }
  if (packet.type === "activity") {
    renderActivity(packet);
    maybeRefreshDiary(packet).catch(() => {});
  }
}

function deskStreamUrl() {
  const token = new URLSearchParams(window.location.search).get("token");
  return token
    ? `/api/desk/stream?token=${encodeURIComponent(token)}`
    : "/api/desk/stream";
}

function subscribeDeskStream() {
  if (state.deskSource) {
    state.deskSource.close();
  }
  const source = new EventSource(deskStreamUrl(), { withCredentials: true });
  source.onmessage = (event) => {
    try {
      handleDeskPacket(JSON.parse(event.data));
    } catch {
      /* ignore keepalives / malformed */
    }
  };
  state.deskSource = source;
}

function subscribeDeskFeed(room) {
  const RoomEvent = window.LivekitClient.RoomEvent;
  const decoder = new TextDecoder();
  room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
    if (topic && topic !== DESK_TOPIC) return;
    let packet;
    try {
      packet = JSON.parse(decoder.decode(payload));
    } catch {
      return;
    }
    if (packet.type !== "transcript" && packet.type !== "activity" && packet.type !== "grounding_violation") return;
    handleDeskPacket(packet);
  });
}

function setCallStatus(message, visible = true) {
  const el = $("call-status");
  el.textContent = message;
  el.classList.toggle("hidden", !visible);
}

async function callAva() {
  // One call at a time. Every /api/token mints a fresh room carrying its own
  // agent dispatch, so a second click while connecting or connected put two
  // Avas in two rooms and both of them talked over each other.
  if (state.connecting || state.room) return;
  state.connecting = true;
  $("call-ava").disabled = true;
  setCallStatus("Connecting to Ava…");
  $("call-ava").classList.add("live");
  let room = null;
  try {
    const token = await api("/api/token", {
      method: "POST",
      body: JSON.stringify({ branch_id: state.branchId }),
    });
    const Room = window.LivekitClient.Room;
    const RoomEvent = window.LivekitClient.RoomEvent;
    room = new Room();
    // Claim the slot before connecting so a click landing mid-connect is
    // refused, and so the handlers below can tell live events from stale ones.
    state.room = room;
    room.on(RoomEvent.TrackSubscribed, (track) => {
      if (track.kind !== "audio" || state.room !== room) return;
      // Replace, never stack: one audible element at a time.
      $("remote-audio").innerHTML = "";
      $("remote-audio").appendChild(track.attach());
    });
    room.on(RoomEvent.Disconnected, () => {
      if (state.room !== room) return;
      hangUp(false);
    });
    subscribeDeskFeed(room);
    await room.connect(token.url, token.token);
    await room.startAudio();
    await room.localParticipant.setMicrophoneEnabled(true);
    $("hang-up").classList.remove("hidden");
    setCallStatus(`Connected to Ava at ${currentBranch().trading_name}. Speak normally.`);
  } catch (error) {
    // Drop a half-connected room, otherwise its dispatched agent keeps talking
    // into a room nobody is listening to.
    if (state.room === room) state.room = null;
    if (room) {
      try {
        await room.disconnect();
      } catch {
        // already gone
      }
    }
    $("call-ava").classList.remove("live");
    $("hang-up").classList.add("hidden");
    $("remote-audio").innerHTML = "";
    setCallStatus(error.message || "Could not connect. Is the agent worker running?");
  } finally {
    state.connecting = false;
    $("call-ava").disabled = Boolean(state.room);
  }
}

async function hangUp(disconnect = true) {
  // Clear the slot first so the Disconnected handler sees a stale room and does
  // not re-enter this function.
  const room = state.room;
  state.room = null;
  if (disconnect && room) {
    try {
      await room.disconnect();
    } catch {
      // already gone
    }
  }
  $("call-ava").classList.remove("live");
  $("call-ava").disabled = false;
  $("hang-up").classList.add("hidden");
  $("remote-audio").innerHTML = "";
  setCallStatus("Call ended.", true);
}

$("call-ava").addEventListener("click", callAva);
$("hang-up").addEventListener("click", () => hangUp(true));
$("diary-date").addEventListener("change", loadDiary);

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("login-password").value }),
    });
    $("login-gate").classList.add("hidden");
    await boot();
  } catch (error) {
    $("login-error").textContent = error.message;
    $("login-error").classList.remove("hidden");
  }
});

async function boot() {
  const config = await api("/api/config");
  $("auth-note").textContent = config.auth_note || "";
  if (config.auth_required) {
    $("login-gate").classList.remove("hidden");
    return;
  }
  const payload = await api("/api/branches");
  state.branches = payload.branches;
  if (!state.branches.some((item) => item.id === state.branchId)) {
    state.branchId = state.branches[0].id;
  }
  $("diary-date").value = $("diary-date").value || todayISO();
  renderTabs();
  renderFacts();
  await loadDiary();
  subscribeDeskStream();
}

boot().catch((error) => {
  setCallStatus(error.message || "Portal failed to load", true);
});
