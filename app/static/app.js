const sections = ["setup", "login", "status"];
const message = document.querySelector("#message");
function show(name) { for (const id of sections) document.querySelector(`#${id}`).classList.toggle("hidden", id !== name); }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...options.headers }, credentials: "same-origin" });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}
async function refresh() {
  message.textContent = "";
  try {
    const who = await api("/api/auth/me");
    const data = await api("/api/system/status");
    const rows = [["Version", data.version], ["Database", data.database], ["Administrators", data.configured_administrators], ["Storage available", data.storage.available_bytes == null ? "Unavailable" : `${(data.storage.available_bytes / 1073741824).toFixed(1)} GiB`]];
    document.querySelector("#status-list").replaceChildren(...rows.flatMap(([k,v]) => { const dt=document.createElement("dt"), dd=document.createElement("dd"); dt.textContent=k; dd.textContent=v; return [dt,dd]; }));
    document.querySelector("#status h2").textContent = `System status · ${who.username}`;
    show("status");
  } catch (error) {
    if (error.message === "Authentication required" || error.message === "Session expired") {
      const state = await api("/api/setup/status"); show(state.setup_required ? "setup" : "login");
    } else { message.textContent = error.message; }
  }
}
document.querySelector("#setup-form").addEventListener("submit", async event => {
  event.preventDefault(); message.textContent = "";
  try { await api("/api/setup/admin", { method:"POST", body:JSON.stringify({ username:document.querySelector("#setup-user").value, password:document.querySelector("#setup-pass").value }) }); await refresh(); }
  catch (error) { message.textContent = error.message; }
});
document.querySelector("#login-form").addEventListener("submit", async event => {
  event.preventDefault(); message.textContent = "";
  try { await api("/api/auth/login", { method:"POST", body:JSON.stringify({ username:document.querySelector("#login-user").value, password:document.querySelector("#login-pass").value }) }); await refresh(); }
  catch (error) { message.textContent = error.message; }
});
document.querySelector("#logout").addEventListener("click", async () => { await api("/api/auth/logout", { method:"POST" }); show("login"); });
refresh();
