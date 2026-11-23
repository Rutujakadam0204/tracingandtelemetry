// Shared by login.html and signup.html
if (Auth.token) location.href = "/";

const form = document.getElementById("auth-form");
const errorEl = document.getElementById("error");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.textContent = "";
  const mode = form.dataset.mode; // "login" or "signup"
  const body = Object.fromEntries(new FormData(form));

  try {
    const data = await api(`/api/auth/${mode}/`, { method: "POST", body });
    Auth.save(data);
    location.href = "/";
  } catch (err) {
    errorEl.textContent = err.message;
  }
});
