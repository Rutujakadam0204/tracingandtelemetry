// Small fetch wrapper that adds the DRF token and handles errors.
const Auth = {
  get token() { return localStorage.getItem("token"); },
  get user() { return JSON.parse(localStorage.getItem("user") || "null"); },
  save(data) {
    localStorage.setItem("token", data.token);
    localStorage.setItem("user", JSON.stringify(data.user));
  },
  clear() {
    localStorage.removeItem("token");
    localStorage.removeItem("user");
  },
};

async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (Auth.token) headers["Authorization"] = `Token ${Auth.token}`;

  const res = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401) {
    Auth.clear();
    if (!location.pathname.startsWith("/login")) location.href = "/login/";
    throw new Error("Not logged in");
  }
  if (res.status === 204) return null;

  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatErrors(data));
  return data;
}

function formatErrors(data) {
  if (data.detail) return data.detail;
  return Object.entries(data)
    .map(([field, msgs]) => `${field}: ${[].concat(msgs).join(" ")}`)
    .join(" | ") || "Something went wrong";
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}
