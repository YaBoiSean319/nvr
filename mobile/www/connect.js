// The app's start screen: it asks once for the NVR's address, keeps it, and from then on opens the NVR's own web interface (all the screens are the NVR's).
// If the person has turned the lock on and has been away longer than the idle time, the phone's biometrics are asked for first.
(function () {
  "use strict";
  var cap = window.Capacitor;
  var plugins = (cap && cap.Plugins) || {};
  var status = document.getElementById("status");
  var form = document.getElementById("connect");
  var problem = document.getElementById("problem");
  var input = document.getElementById("address");

  function get(key) {
    return plugins.Preferences ? plugins.Preferences.get({ key: key }).then(function (r) { return r.value; }) : Promise.resolve(null);
  }
  function set(key, value) {
    return plugins.Preferences ? plugins.Preferences.set({ key: key, value: value }) : Promise.resolve();
  }
  function say(text) { problem.hidden = !text; problem.textContent = text || ""; }

  // A private-network host: a name with no dots, *.local, localhost, or a private, loopback or link-local IP address (checked octet by octet, so "10.evil.com" is not one).
  function isPrivateHost(host) {
    if (host === "localhost" || host === "[::1]" || /^[^.:\[]+$/.test(host) || /^[^.]+\.local$/.test(host)) return true;
    var octets = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
    if (!octets) return false;
    var n = octets.slice(1).map(Number);
    if (n.some(function (part) { return part > 255; })) return false;
    return n[0] === 10 || n[0] === 127 || (n[0] === 192 && n[1] === 168) || (n[0] === 172 && n[1] >= 16 && n[1] <= 31) || (n[0] === 169 && n[1] === 254);
  }

  // https anywhere; plain http only for a private network address, which cannot be reached from outside.
  function acceptable(address) {
    var url;
    try { url = new URL(address); } catch (e) { return null; }
    if (url.username || url.password || (url.protocol !== "https:" && url.protocol !== "http:")) return null;
    if (url.protocol === "http:" && !isPrivateHost(url.hostname)) return null;
    return url.origin;
  }

  function open(address) {
    status.textContent = "Opening your NVR…";
    window.location.replace(address);
  }

  function unlockFirst() {
    return Promise.all([get("lock.enabled"), get("lock.idle_minutes"), get("lock.last_active")]).then(function (values) {
      var idle = Number(values[1]) || 5;
      var last = Number(values[2]) || 0;
      var away = Date.now() - last >= idle * 60000;
      if (values[0] !== "1" || !away || !plugins.NativeBiometric) return true;
      return plugins.NativeBiometric.verifyIdentity({ reason: "Unlock the NVR", title: "NVR", useFallback: true })
        // Unlocked here, so the interface that opens next does not ask again.
        .then(function () { return set("lock.last_active", String(Date.now())).then(function () { return true; }); }, function () { return false; });
    });
  }

  function start() {
    get("server.url").then(function (address) {
      if (!address) { status.hidden = true; form.hidden = false; return; }
      unlockFirst().then(function (ok) {
        if (ok) { open(address); return; }
        status.textContent = "NVR is locked.";
        var retry = document.createElement("button");
        retry.textContent = "Unlock";
        retry.onclick = function () { window.location.reload(); };
        status.parentNode.insertBefore(retry, status.nextSibling);
      });
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    say("");
    var address = acceptable(input.value.trim());
    if (!address) { say("Enter an https:// address, or an http:// one on your own network."); return; }
    status.hidden = false;
    status.textContent = "Checking…";
    var check = plugins.CapacitorHttp
      ? plugins.CapacitorHttp.get({ url: address + "/api/setup/status", connectTimeout: 8000, readTimeout: 8000 }).then(function (r) { return r.status === 200 && r.data && typeof r.data.setup_required === "boolean"; })
      : fetch(address + "/api/setup/status").then(function (r) { return r.json(); }).then(function (d) { return typeof d.setup_required === "boolean"; });
    check.then(function (ok) {
      if (!ok) throw new Error("not an NVR");
      return set("server.url", address).then(function () { open(address); });
    }).catch(function () {
      status.hidden = true;
      say("That address did not answer like an NVR. Check it and that this phone can reach it.");
    });
  });

  start();
})();
