const state = {
  branchId: "shellharbour",
  branches: [],
  diary: { slots: [], bookings: [] },
  room: null,
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
  return new Date().toLocaleDateString("en-CA", { timeZone: "Australia/Sydney" });
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
    row.style.setProperty("--i", String(list.children.length));
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

function setCallStatus(message, visible = true) {
  const el = $("call-status");
  el.textContent = message;
  el.classList.toggle("hidden", !visible);
}

function livekitSdk() {
  const sdk =
    globalThis.LivekitClient ||
    globalThis.LiveKitClient ||
    globalThis.livekitClient;
  if (!sdk || !sdk.Room) {
    throw new Error("LiveKit browser SDK did not load. Refresh and try Call Ava again.");
  }
  return sdk;
}

function isAudioTrack(track) {
  if (!track) return false;
  const kind = String(track.kind || "").toLowerCase();
  return kind === "audio";
}

function attachRemoteAudio(track) {
  // Docs: https://docs.livekit.io/transport/media/subscribe/
  if (!isAudioTrack(track)) return;
  const mount = $("remote-audio");
  const el = track.attach();
  el.autoplay = true;
  el.playsInline = true;
  el.setAttribute("autoplay", "");
  el.setAttribute("playsinline", "");
  el.muted = false;
  if (!el.isConnected) {
    mount.appendChild(el);
  }
  const playAttempt = el.play();
  if (playAttempt && typeof playAttempt.catch === "function") {
    playAttempt.catch(() => {});
  }
}

function attachExistingRemoteAudio(room) {
  const participants = room.remoteParticipants;
  if (!participants || typeof participants.forEach !== "function") return;
  participants.forEach((participant) => {
    const pubs = participant.trackPublications || participant.tracks;
    if (!pubs || typeof pubs.forEach !== "function") return;
    pubs.forEach((publication) => {
      if (publication && publication.track) {
        attachRemoteAudio(publication.track);
      }
    });
  });
}

async function unlockAudioPlayback() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((track) => track.stop());
  } catch {
    // Permission prompt may appear again when the microphone is published.
  }
}

async function callAva() {
  setCallStatus("Connecting to Ava…");
  $("call-ava").classList.add("live");
  document.body.classList.add("call-live");
  try {
    const { Room, RoomEvent } = livekitSdk();
    await unlockAudioPlayback();
    const token = await api("/api/token", {
      method: "POST",
      body: JSON.stringify({ branch_id: state.branchId }),
    });
    const room = new Room();
    room.on(RoomEvent.TrackSubscribed, (track) => {
      attachRemoteAudio(track);
    });
    room.on(RoomEvent.TrackUnsubscribed, (track) => {
      if (!isAudioTrack(track) || typeof track.detach !== "function") return;
      for (const el of track.detach()) {
        el.remove();
      }
    });
    if (RoomEvent.AudioPlaybackStatusChanged) {
      room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
        if (room.canPlaybackAudio === false) {
          room.startAudio().catch(() => {});
        }
      });
    }
    room.on(RoomEvent.Disconnected, () => hangUp(false));
    await room.connect(token.url, token.token);
    if (typeof room.startAudio === "function") {
      await room.startAudio();
    }
    attachExistingRemoteAudio(room);
    await room.localParticipant.setMicrophoneEnabled(true);
    state.room = room;
    $("hang-up").classList.remove("hidden");
    setCallStatus(
      `Connected to Ava at ${currentBranch().trading_name}. Speak normally.`
    );
  } catch (error) {
    $("call-ava").classList.remove("live");
    document.body.classList.remove("call-live");
    setCallStatus(error.message || "Could not connect. Is the agent worker running?");
  }
}

async function hangUp(disconnect = true) {
  if (disconnect && state.room) {
    await state.room.disconnect();
  }
  state.room = null;
  $("call-ava").classList.remove("live");
  document.body.classList.remove("call-live");
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
}

boot().catch((error) => {
  setCallStatus(error.message || "Portal failed to load", true);
});
