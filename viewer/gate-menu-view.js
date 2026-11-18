/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// The gate action menu as a pop-up over the scene. What it offers comes from gate-menu.js.
const CLOSE_AFTER_MS = 8000;

// `items(gate)` returns the menu for a gate; `run(command)` sends one
export function createGateMenu(container, {items, run}) {
  const menu = document.createElement('div');
  menu.id = 'gate-menu';
  menu.hidden = true;
  menu.setAttribute('role', 'menu');
  container.append(menu);
  let gate = null, timer = null;

  function close() {
    clearTimeout(timer);
    menu.hidden = true;
    gate = null;
  }
  function fill() {
    const title = document.createElement('div');
    title.className = 'gate-menu-title';
    title.textContent = `Gate ${gate}`;
    const buttons = items(gate).map(item => {
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('role', 'menuitem');
      button.dataset.item = item.id;
      button.disabled = item.disabled;
      button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${item.icon}"/></svg>`;
      button.append(item.label);
      button.onclick = () => { close(); run(item.command); };
      return button;
    });
    menu.replaceChildren(title, ...buttons);
  }
  // Opens at a point given in page coordinates, kept inside the container
  function open(forGate, clientX, clientY) {
    clearTimeout(timer);
    gate = forGate;
    fill();
    menu.hidden = false;
    const bounds = container.getBoundingClientRect();
    const left = Math.max(0, Math.min(clientX-bounds.left-20, bounds.width-menu.offsetWidth));
    const top = Math.max(0, Math.min(clientY-bounds.top-20, bounds.height-menu.offsetHeight));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    timer = setTimeout(close, CLOSE_AFTER_MS);
  }
  // The printer's state changed: what can be sent may have changed with it
  function refresh() {
    if (gate !== null) fill();
  }

  menu.addEventListener('mouseleave', close);
  document.addEventListener('pointerdown', event => { if (gate !== null && !menu.contains(event.target)) close(); }, true);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
  return {open, close, refresh};
}
