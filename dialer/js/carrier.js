/* Carrier adapter. The cockpit only ever talks to this interface, so moving
   from Twilio to Telnyx (or anything WebRTC) means adding one adapter here
   and one token route on the server; nothing else changes.

     carrier.name                      "twilio" | "simulator"
     carrier.real                      true when calls reach the phone network
     carrier.connect(to, callerId)  -> Promise<Call>
     carrier.refresh(token)            new access token
     carrier.audio.setInput(id) / setOutput(id) / outputSupported

     Call.on(evt, fn)   ringing | answered | ended(reason) | error(msg)
                        | warning(name) | warning-cleared
     Call.hangup() .mute(bool) .digits(str) .sid()
     ended reasons:     "hangup" (after answer) | "no_answer" | "voicemail"

   Incoming calls arrive through hooks.onIncoming(IncomingCall) with
     .from  .accept() -> Call  .reject()  .onCancel(fn)
*/

function emitter() {
  const l = {};
  return {
    on(evt, fn) { (l[evt] = l[evt] || []).push(fn); return this; },
    fire(evt, arg) { (l[evt] || []).forEach((fn) => fn(arg)); }
  };
}

/* ------------------------------------------------------------ simulator -- */

export function simulatorCarrier() {
  let rng = 7;                          // deterministic: demos are repeatable
  return {
    name: "simulator", real: false,
    audio: { setInput() {}, setOutput() {}, outputSupported: false },
    refresh() {},
    connect() {
      const e = emitter();
      let answered = false, over = false;
      const end = (reason) => { if (!over) { over = true; e.fire("ended", reason); } };
      setTimeout(() => { if (!over) e.fire("ringing"); }, 600);
      setTimeout(() => {
        if (over) return;
        rng = (rng * 7 + 3) % 10;       // ~30% pickup
        if (rng < 3) { answered = true; e.fire("answered"); } else end(rng < 6 ? "voicemail" : "no_answer");
      }, 2100);
      return Promise.resolve({
        on: e.on.bind(e),
        hangup() { end(answered ? "hangup" : "no_answer"); },
        mute() {}, digits() {}, sid() { return ""; }
      });
    }
  };
}

/* --------------------------------------------------------------- twilio -- */

function wrapTwilioCall(call, alreadyAnswered) {
  const e = emitter();
  let answered = !!alreadyAnswered, over = false;
  const end = (reason) => { if (!over) { over = true; e.fire("ended", reason); } };
  call.on("ringing", () => e.fire("ringing"));
  call.on("accept", () => { answered = true; e.fire("answered"); });
  call.on("disconnect", () => end(answered ? "hangup" : "no_answer"));
  call.on("cancel", () => end("no_answer"));
  call.on("error", (err) => { e.fire("error", err && err.message); end(answered ? "hangup" : "no_answer"); });
  call.on("warning", (name) => e.fire("warning", name));
  call.on("warning-cleared", () => e.fire("warning-cleared"));
  return {
    on: e.on.bind(e),
    hangup() { call.disconnect(); },
    mute(on) { call.mute(on); },
    digits(d) { call.sendDigits(d); },
    sid() { return (call.parameters && call.parameters.CallSid) || ""; }
  };
}

export function twilioCarrier(token, hooks) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "/twilio.min.js";                       // vendored @twilio/voice-sdk
    s.onerror = () => reject(new Error("Twilio SDK failed to load"));
    s.onload = () => {
      const device = new Twilio.Device(token, { logLevel: "error" });
      device.on("error", (err) => hooks.onError && hooks.onError(err.message));
      device.on("tokenWillExpire", () => hooks.onTokenExpiring && hooks.onTokenExpiring());
      device.on("registered", () => hooks.onReady && hooks.onReady());
      device.on("incoming", (call) => {
        const cancels = [];
        call.on("cancel", () => cancels.forEach((fn) => fn()));
        hooks.onIncoming({
          from: (call.parameters && call.parameters.From) || "",
          accept() { call.accept(); return wrapTwilioCall(call, true); },
          reject() { call.reject(); },
          onCancel(fn) { cancels.push(fn); }
        });
      });
      device.register();
      resolve({
        name: "twilio", real: true,
        refresh(t) { device.updateToken(t); },
        connect(to, callerId) {
          return device.connect({ params: { To: to, CallerId: callerId } }).then((c) => wrapTwilioCall(c, false));
        },
        audio: {
          get outputSupported() { return !!(device.audio && device.audio.isOutputSelectionSupported); },
          setInput(id) { try { return device.audio.setInputDevice(id).catch(() => {}); } catch (e) {} },
          setOutput(id) { try { return device.audio.speakerDevices.set([id]).catch(() => {}); } catch (e) {} }
        }
      });
    };
    document.head.appendChild(s);
  });
}
