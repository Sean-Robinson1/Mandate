// Crosshair and tooltip for the stock chart: pointer or arrow keys move between days.
document.querySelectorAll("svg[data-hover]").forEach(svg => {
  const points = JSON.parse(svg.dataset.hover), W = +svg.dataset.w, tip = svg.parentElement.querySelector(".chart-tip");
  if (!points.length) return;
  const NS = "http://www.w3.org/2000/svg";
  const line = document.createElementNS(NS, "line"), dot = document.createElementNS(NS, "circle");
  line.setAttribute("class", "cross"); dot.setAttribute("class", "cross-dot"); dot.setAttribute("r", 4.5);
  line.setAttribute("y1", 0); line.setAttribute("y2", svg.viewBox.baseVal.height);
  svg.append(line, dot);
  let index = points.length - 1;
  const show = i => {
    index = Math.max(0, Math.min(points.length - 1, i));
    const p = points[index], scale = svg.getBoundingClientRect().width / W;
    line.setAttribute("x1", p.x); line.setAttribute("x2", p.x);
    dot.setAttribute("cx", p.x); dot.setAttribute("cy", p.y);
    line.style.display = dot.style.display = "";
    tip.hidden = false;
    tip.innerHTML = "<span></span><strong></strong>";
    tip.children[0].textContent = p.t; tip.children[1].textContent = p.v;
    const x = p.x * scale;
    tip.style.left = Math.min(x + 12, svg.clientWidth - tip.offsetWidth) + "px";
    tip.style.top = Math.max(0, p.y * scale - 48) + "px";
  };
  const hide = () => { tip.hidden = true; line.style.display = dot.style.display = "none"; };
  hide();
  svg.addEventListener("pointermove", e => {
    const x = (e.clientX - svg.getBoundingClientRect().left) / svg.getBoundingClientRect().width * W;
    let best = 0;
    points.forEach((p, i) => { if (Math.abs(p.x - x) < Math.abs(points[best].x - x)) best = i; });
    show(best);
  });
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("focus", () => show(index));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); show(index + (e.key === "ArrowRight" ? 1 : -1)); }
  });
});
