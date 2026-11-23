if (!Auth.token) location.href = "/login/";

const me = Auth.user;
const form = document.getElementById("task-form");
const formTitle = document.getElementById("form-title");
const formError = document.getElementById("form-error");
const saveBtn = document.getElementById("save-btn");
const cancelBtn = document.getElementById("cancel-btn");
const assignSelect = form.elements["assigned_to"];

let tasksById = {};

// ---------- header ----------
document.getElementById("userbox").innerHTML = `
  <span>Hi, <strong>${escapeHtml(me?.username)}</strong></span>
  <button class="btn small" id="logout-btn">Log out</button>`;
document.getElementById("logout-btn").onclick = async () => {
  try { await api("/api/auth/logout/", { method: "POST" }); } catch (_) {}
  Auth.clear();
  location.href = "/login/";
};

// ---------- users dropdown ----------
async function loadUsers() {
  const users = await api("/api/auth/users/");
  assignSelect.innerHTML =
    `<option value="">— Nobody —</option>` +
    users.map((u) => `<option value="${u.id}">${escapeHtml(u.username)}${u.id === me.id ? " (me)" : ""}</option>`).join("");
}

// ---------- task list ----------
function renderTask(t) {
  const meta = t.is_owner
    ? `Assigned to: ${escapeHtml(t.assigned_to_username) || "nobody"}`
    : `Created by: ${escapeHtml(t.created_by_username)}`;
  return `
    <li>
      <div class="title">${escapeHtml(t.task_name)}</div>
      ${t.task_desc ? `<div class="desc">${escapeHtml(t.task_desc)}</div>` : ""}
      <div class="meta">${meta} · updated ${new Date(t.updated_at).toLocaleString()}</div>
      <div class="row">
        <button class="btn small" data-edit="${t.id}">Edit</button>
        ${t.is_owner ? `<button class="btn small danger" data-delete="${t.id}">Delete</button>` : ""}
      </div>
    </li>`;
}

function renderList(el, countEl, tasks) {
  countEl.textContent = tasks.length;
  el.innerHTML = tasks.length ? tasks.map(renderTask).join("") : `<li class="empty">No tasks</li>`;
}

async function loadTasks() {
  const tasks = await api("/api/tasks/");
  tasksById = Object.fromEntries(tasks.map((t) => [t.id, t]));
  // A task I created and assigned to myself shows in "My tasks" only.
  renderList(document.getElementById("my-tasks"), document.getElementById("my-count"),
    tasks.filter((t) => t.is_owner));
  renderList(document.getElementById("assigned-tasks"), document.getElementById("assigned-count"),
    tasks.filter((t) => !t.is_owner));
}

// ---------- form: create / edit ----------
function resetForm() {
  form.reset();
  form.elements["id"].value = "";
  formTitle.textContent = "New task";
  saveBtn.textContent = "Add task";
  cancelBtn.hidden = true;
  assignSelect.disabled = false;
  formError.textContent = "";
}

function startEdit(task) {
  form.elements["id"].value = task.id;
  form.elements["task_name"].value = task.task_name;
  form.elements["task_desc"].value = task.task_desc;
  assignSelect.value = task.assigned_to ?? "";
  // Only the creator can reassign
  assignSelect.disabled = !task.is_owner;
  formTitle.textContent = `Edit: ${task.task_name}`;
  saveBtn.textContent = "Save changes";
  cancelBtn.hidden = false;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  formError.textContent = "";
  const id = form.elements["id"].value;
  const body = {
    task_name: form.elements["task_name"].value,
    task_desc: form.elements["task_desc"].value,
  };
  if (!assignSelect.disabled) body.assigned_to = assignSelect.value ? Number(assignSelect.value) : null;

  try {
    if (id) await api(`/api/tasks/${id}/`, { method: "PATCH", body });
    else await api("/api/tasks/", { method: "POST", body });
    resetForm();
    await loadTasks();
  } catch (err) {
    formError.textContent = err.message;
  }
});

cancelBtn.onclick = resetForm;

document.addEventListener("click", async (e) => {
  const editId = e.target.dataset.edit;
  const deleteId = e.target.dataset.delete;
  if (editId) startEdit(tasksById[editId]);
  if (deleteId && confirm("Delete this task?")) {
    await api(`/api/tasks/${deleteId}/`, { method: "DELETE" });
    if (form.elements["id"].value === deleteId) resetForm();
    await loadTasks();
  }
});

// ---------- init ----------
loadUsers().then(loadTasks).catch((err) => (formError.textContent = err.message));
