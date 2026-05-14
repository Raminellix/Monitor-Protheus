(function markActiveNav(){
  const path = window.location.pathname;
  document.querySelectorAll(".nav a").forEach(a => {
    try{
      const href = a.getAttribute("href") || "";
      if (href && href !== "#" && path === new URL(href, window.location.origin).pathname) {
        a.classList.add("active");
      }
    }catch{}
  });
})();