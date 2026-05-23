/**
 * Kim Bridge — overlay.js
 *
 * Injected on all four AI chat sites.
 * Adds file drag-and-drop support: when the user drops a file onto the page,
 * the extension reads it via FileReader and sends its content to the Kim
 * bridge as a write_file call.
 *
 * A small toast notification is shown on success or failure.
 */

(function kimOverlay() {
  "use strict";

  // ── Toast notification ────────────────────────────────────────────────────

  function showToast(message, isError = false) {
    const existing = document.getElementById("kim-toast");
    if (existing) existing.remove();

    const toast = document.createElement("div");
    toast.id = "kim-toast";
    toast.textContent = message;
    Object.assign(toast.style, {
      position:      "fixed",
      bottom:        "24px",
      right:         "24px",
      zIndex:        "2147483647",
      padding:       "10px 16px",
      borderRadius:  "8px",
      fontSize:      "13px",
      fontFamily:    "system-ui, sans-serif",
      color:         "#fff",
      background:    isError ? "#c0392b" : "#27ae60",
      boxShadow:     "0 4px 12px rgba(0,0,0,0.3)",
      transition:    "opacity 0.4s",
      opacity:       "1",
      pointerEvents: "none",
    });
    document.body.appendChild(toast);
    setTimeout(() => { toast.style.opacity = "0"; }, 2800);
    setTimeout(() => toast.remove(), 3300);
  }

  // ── Drag-and-drop logic ───────────────────────────────────────────────────

  /**
   * Read a File object and POST its content to the Kim bridge as write_file.
   * The file is saved to the project root using the original filename.
   */
  async function handleDroppedFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();

      reader.onload = async (e) => {
        const content = e.target.result;
        const path = file.name; // relative to PROJECT_ROOT on the server

        try {
          const resp = await chrome.runtime.sendMessage({
            type:    "KIM_WRITE_FILE",
            path:    path,
            content: content,
          });
          if (resp.ok) {
            showToast(`✓ ${file.name} → project root`);
            resolve();
          } else {
            showToast(`✗ Failed: ${resp.error || "unknown"}`, true);
            reject(new Error(resp.error));
          }
        } catch (err) {
          showToast(`✗ Bridge error: ${err.message}`, true);
          reject(err);
        }
      };

      reader.onerror = () => {
        showToast(`✗ Could not read ${file.name}`, true);
        reject(new Error("FileReader error"));
      };

      // Read as Data URL (base64) so binary files are safely encoded
      // and text files are also preserved without UTF-8 decoder replacement corruption.
      reader.readAsDataURL(file);
    });
  }

  // ── Event listeners ───────────────────────────────────────────────────────

  document.addEventListener("dragover", (e) => {
    // Only intercept if files are being dragged
    if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }, true);

  document.addEventListener("drop", async (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;

    // Only intercept if at least one file is present
    e.preventDefault();
    e.stopPropagation();

    const toSync = Array.from(files);
    showToast(`Syncing ${toSync.length} file(s) to Kim…`);

    for (const file of toSync) {
      try {
        await handleDroppedFile(file);
      } catch (_) {
        // Individual error toasts shown inside handleDroppedFile
      }
    }
  }, true);

  console.log("[Kim] Overlay (drag-and-drop) loaded");
})();
