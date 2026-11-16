// Arriba: allgemeine Bedienung (Kopieren, Farbschema, Formulare)
(() => {
  "use strict";

  // Kopierknöpfe
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (!b || !navigator.clipboard) return;
    navigator.clipboard.writeText(b.dataset.copy).then(() => {
      b.classList.add("is-copied");
      setTimeout(() => b.classList.remove("is-copied"), 1600);
    });
  });

  // Farbschema: automatisch → hell → dunkel
  const root = document.documentElement;
  const order = ["auto", "light", "dark"];
  const labels = { auto: "Farbschema: automatisch", light: "Farbschema: hell", dark: "Farbschema: dunkel" };
  const current = () => root.dataset.theme || "auto";
  const toggle = document.querySelector("[data-theme-toggle]");
  const label = () => { if (toggle) { toggle.title = labels[current()]; toggle.setAttribute("aria-label", labels[current()]); } };
  label();
  toggle?.addEventListener("click", () => {
    const next = order[(order.indexOf(current()) + 1) % order.length];
    if (next === "auto") delete root.dataset.theme; else root.dataset.theme = next;
    try { next === "auto" ? localStorage.removeItem("theme") : localStorage.setItem("theme", next); } catch (e) { /* egal */ }
    label();
    document.dispatchEvent(new CustomEvent("themechange"));
  });

  // Rückfrage vor dem Löschen
  document.addEventListener("submit", (e) => {
    const message = e.target.dataset.confirm;
    if (message && !confirm(message)) e.preventDefault();
  });

  // Datei-Feld mit Ziehen und Ablegen
  const drop = document.getElementById("drop");
  if (drop) {
    const input = drop.querySelector("input");
    const label = document.getElementById("dz-file");
    const show = () => {
      const f = input.files[0];
      drop.classList.toggle("has-file", !!f);
      label.textContent = f
        ? `${f.name} · ${(f.size / 1024).toLocaleString("de-DE", { maximumFractionDigits: 0 })} KB`
        : label.dataset.hint;
    };
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("is-over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("is-over")));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); }
    });
  }

  // Vorschau des Profilbilds
  const avatar = document.getElementById("avatar");
  avatar?.addEventListener("change", () => {
    const f = avatar.files[0];
    if (f) document.getElementById("avatar-preview").src = URL.createObjectURL(f);
  });

  // Übungen im Workout-Formular
  const rows = document.getElementById("ex-rows");
  if (rows) {
    const tpl = document.getElementById("ex-template");
    const total = document.getElementById("workout-total");
    const parse = (text) => {
      text = text.trim().toLowerCase();
      if (!text) return [];
      const m = text.match(/^(\d+)\s*[x×*]\s*(\d+)$/);
      if (m) return Array(Math.min(+m[1], 50)).fill(+m[2]);
      const parts = text.split(/[\s,;/·+]+/).filter(Boolean);
      return parts.every((p) => /^\d+$/.test(p)) ? parts.map(Number) : null;
    };
    const sum = (a) => a.reduce((x, y) => x + y, 0);
    const update = (row) => {
      const sets = parse(row.querySelector(".ex-sets").value);
      const unit = row.querySelector(".ex-unit").value === "sec" ? "Sek." : "Wdh.";
      row.classList.toggle("is-invalid", sets === null);
      row.querySelector(".ex-preview").textContent = sets === null
        ? "Format: 8 8 7 6 oder 4x8"
        : sets.length ? `${sets.length} ${sets.length === 1 ? "Satz" : "Sätze"} · ${sum(sets)} ${unit}` : "";
    };
    const updateTotal = () => {
      let sets = 0, reps = 0, hold = 0;
      rows.querySelectorAll(".ex-row").forEach((row) => {
        const s = parse(row.querySelector(".ex-sets").value) || [];
        sets += s.length;
        if (row.querySelector(".ex-unit").value === "sec") hold += sum(s); else reps += sum(s);
      });
      total.textContent = sets
        ? `Gesamt: ${sets} ${sets === 1 ? "Satz" : "Sätze"}${reps ? ` · ${reps} Wdh.` : ""}${hold ? ` · ${hold} Sek.` : ""}`
        : "";
    };
    const changed = (e) => { const r = e.target.closest(".ex-row"); if (r) { update(r); updateTotal(); } };
    rows.addEventListener("input", changed);
    rows.addEventListener("change", changed);
    rows.addEventListener("click", (e) => {
      const btn = e.target.closest(".ex-remove");
      if (!btn) return;
      const row = btn.closest(".ex-row");
      if (rows.children.length > 1) row.remove();
      else { row.querySelectorAll("input").forEach((i) => (i.value = "")); update(row); }
      updateTotal();
    });
    const add = () => {
      rows.appendChild(tpl.content.cloneNode(true));
      return rows.lastElementChild;
    };
    document.getElementById("add-ex").addEventListener("click", () => add().querySelector(".ex-name").focus());
    // Eingabetaste im letzten Sätze-Feld legt die nächste Übung an
    rows.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !e.target.matches("input")) return;
      e.preventDefault();
      const row = e.target.closest(".ex-row");
      const next = row.nextElementSibling || add();
      next.querySelector(".ex-name").focus();
    });
    rows.querySelectorAll(".ex-row").forEach(update);
    updateTotal();
  }
})();
