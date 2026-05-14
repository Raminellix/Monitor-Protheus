async function deleteServer(id){
  await fetch(`/services/delete-server/${id}`, {method:"POST"});
  window.location.reload();
}

async function svcAction(server, service_name, action){
  const res = await fetch("/services/action", {
    method:"POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({server, service_name, action})
  });
  const data = await res.json();
  alert(data.message || "OK");
}

document.addEventListener("DOMContentLoaded", () => {
  const f = document.getElementById("addServerForm");
  if(!f) return;
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const formData = new FormData(f);
    const payload = Object.fromEntries(formData.entries());
    const res = await fetch("/services/add-server", {
      method:"POST",
      headers: {"Content-Type":"application/x-www-form-urlencoded"},
      body: new URLSearchParams(payload)
    });
    if(res.ok) window.location.reload();
    else alert("Erro ao adicionar servidor.");
  });
});